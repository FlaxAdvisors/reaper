"""order.db: per-rack SQLite store (devices, MACs, rung runs, events, gate waivers)."""
import dataclasses
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS device(
  pos TEXT PRIMARY KEY, kind TEXT, host_mac TEXT, bmc_mac TEXT,
  iom_macs TEXT NOT NULL DEFAULT '[]', pop_override TEXT,
  bmc_reachable INTEGER, bmc_power TEXT, bmc_at TEXT);
CREATE TABLE IF NOT EXISTS run(
  id INTEGER PRIMARY KEY, pos TEXT NOT NULL, stage TEXT NOT NULL, rung TEXT NOT NULL,
  status TEXT NOT NULL, verdict TEXT, detail TEXT, artifacts TEXT,
  started_at TEXT NOT NULL, finished_at TEXT);
CREATE INDEX IF NOT EXISTS run_pos_rung ON run(pos, rung, id);
CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS waiver(
  id INTEGER PRIMARY KEY, pos TEXT NOT NULL, stage TEXT NOT NULL, rungs TEXT NOT NULL,
  reason TEXT NOT NULL, who TEXT NOT NULL, at TEXT NOT NULL);
"""
# columns added after Plan 1 shipped: an existing order.db gains them on init
_ADDED = (("device", "bmc_reachable", "INTEGER"), ("device", "bmc_power", "TEXT"),
          ("device", "bmc_at", "TEXT"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_mac(m: str | None) -> str | None:
    if m is None:
        return None
    h = re.sub(r"[^0-9a-fA-F]", "", m).lower()
    if len(h) != 12:
        raise ValueError(f"bad MAC {m!r}")
    return h


class OrderDB:
    def __init__(self, path):
        self.path = Path(path)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        """Run one write statement in its own transaction; returns the cursor."""
        with closing(self._conn()) as c, c:
            return c.execute(sql, args)

    def _all(self, sql: str, args=()) -> list[sqlite3.Row]:
        with closing(self._conn()) as c:
            return c.execute(sql, args).fetchall()

    def init(self, g) -> None:
        with closing(self._conn()) as c, c:
            c.executescript(_SCHEMA)
            for table, col, typ in _ADDED:
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            for k, v in (("order", g.order), ("customer", g.customer),
                         ("geometry", json.dumps(dataclasses.asdict(g)))):
                c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
            for d in g.devices:
                c.execute("INSERT OR IGNORE INTO device(pos,kind) VALUES(?,?)", (d.pos, d.kind))

    def set_macs(self, pos, *, host_mac, bmc_mac, iom_macs) -> None:
        cur = self._exec(
            "UPDATE device SET host_mac=?, bmc_mac=?, iom_macs=? WHERE pos=?",
            (norm_mac(host_mac), norm_mac(bmc_mac),
             json.dumps([norm_mac(m) for m in iom_macs]), pos))
        if cur.rowcount == 0:
            raise KeyError(pos)

    def set_pop(self, pos, profile) -> None:
        cur = self._exec("UPDATE device SET pop_override=? WHERE pos=?", (profile, pos))
        if cur.rowcount == 0:
            raise KeyError(pos)

    def set_bmc_live(self, pos, reachable: bool, power: str | None, at: str) -> None:
        """The BMC poller's last look (not a rung run: never creates a run row)."""
        cur = self._exec("UPDATE device SET bmc_reachable=?, bmc_power=?, bmc_at=? WHERE pos=?",
                         (int(bool(reachable)), power, at, pos))
        if cur.rowcount == 0:
            raise KeyError(pos)

    def start_run(self, pos, stage, rung) -> int:
        return self._exec(
            "INSERT INTO run(pos,stage,rung,status,started_at) VALUES(?,?,?,'running',?)",
            (pos, stage, rung, _now())).lastrowid

    def finish_run(self, run_id, result) -> None:
        # only a still-running row is closed; finished runs are never rewritten
        self._exec(
            "UPDATE run SET status='done', verdict=?, detail=?, artifacts=?, finished_at=? "
            "WHERE id=? AND status='running'",
            (result.verdict, json.dumps(result.detail), json.dumps(result.artifacts),
             _now(), run_id))

    def latest_runs(self) -> dict:
        rows = self._all(
            "SELECT * FROM run WHERE id IN (SELECT MAX(id) FROM run GROUP BY pos, rung)")
        return {(r["pos"], r["rung"]): {
            "stage": r["stage"], "status": r["status"], "verdict": r["verdict"],
            "detail": json.loads(r["detail"]) if r["detail"] else None,
            "artifacts": json.loads(r["artifacts"]) if r["artifacts"] else {},
            "started_at": r["started_at"], "finished_at": r["finished_at"]} for r in rows}

    def run_count(self, pos, rung) -> int:
        return self._all("SELECT COUNT(*) n FROM run WHERE pos=? AND rung=?", (pos, rung))[0]["n"]

    def mark_interrupted(self) -> int:
        # an interrupted run keeps its row; a new start_run appends a fresh one
        return self._exec(
            "UPDATE run SET status='interrupted', finished_at=? WHERE status='running'",
            (_now(),)).rowcount

    def devices(self) -> list[dict]:
        return [{"pos": r["pos"], "kind": r["kind"], "host_mac": r["host_mac"],
                 "bmc_mac": r["bmc_mac"], "iom_macs": json.loads(r["iom_macs"]),
                 "pop_override": r["pop_override"],
                 "bmc_live": {"reachable": None if r["bmc_reachable"] is None
                              else bool(r["bmc_reachable"]),
                              "power": r["bmc_power"], "at": r["bmc_at"]}}
                for r in self._all("SELECT * FROM device ORDER BY pos")]

    def add_event(self, kind, text) -> None:
        self._exec("INSERT INTO event(ts,kind,text) VALUES(?,?,?)", (_now(), kind, text))

    def events(self, limit=50) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT ts,kind,text FROM event ORDER BY id DESC LIMIT ?", (limit,))]

    def set_qualify_progress(self, progress: dict | None) -> None:
        """Qualify's current phase and barrier (None = no Qualify running)."""
        self._set_meta_json("qualify_progress", progress)

    def qualify_progress(self) -> dict | None:
        return self._meta_json("qualify_progress")

    def set_deploy_progress(self, progress: dict | None) -> None:
        """Deploy's current phase and barrier (None = no Deploy running)."""
        self._set_meta_json("deploy_progress", progress)

    def deploy_progress(self) -> dict | None:
        return self._meta_json("deploy_progress")

    def set_ha_mode(self, ha: dict | None) -> None:
        """The HA mode Deploy's ha-test delivered (order level; None clears it)."""
        self._set_meta_json("ha_mode", ha)

    def ha_mode(self) -> dict | None:
        return self._meta_json("ha_mode")

    def _set_meta_json(self, k, v) -> None:
        if v is None:
            self._exec("DELETE FROM meta WHERE k=?", (k,))
        else:
            self._exec("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v)))

    def _meta_json(self, k):
        r = self._all("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else None

    def add_waiver(self, pos, stage, rungs: list[str], reason: str, who: str) -> dict:
        """Record an operator's waiver of `rungs` (failed Qualify rungs) for `stage`'s gate.
        Rows are never rewritten: the history is the record."""
        at = _now()
        self._exec("INSERT INTO waiver(pos,stage,rungs,reason,who,at) VALUES(?,?,?,?,?,?)",
                   (pos, stage, json.dumps(list(rungs)), reason, who, at))
        return {"pos": pos, "stage": stage, "rungs": list(rungs), "reason": reason, "who": who,
                "at": at}

    def waivers(self, history=False) -> dict | list:
        """pos -> its latest waiver ({pos, stage, rungs, reason, who, at}); every row,
        oldest first, with history=True."""
        rows = [{"pos": r["pos"], "stage": r["stage"], "rungs": json.loads(r["rungs"]),
                 "reason": r["reason"], "who": r["who"], "at": r["at"]}
                for r in self._all("SELECT * FROM waiver ORDER BY id")]
        return rows if history else {r["pos"]: r for r in rows}

    def set_closed(self) -> None:
        self._exec("INSERT OR IGNORE INTO meta(k,v) VALUES('closed_at',?)", (_now(),))

    def closed_at(self) -> str | None:
        r = self._all("SELECT v FROM meta WHERE k='closed_at'")
        return r[0]["v"] if r else None
