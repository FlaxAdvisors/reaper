"""order.db: per-rack SQLite store (devices, MACs, rung runs, events, gate waivers,
Done's component list, the live link monitor's state transitions, passes).

A pass is one go at the order. Start fresh opens a new pass: it records the highest run
and waiver id so far, and every "latest" read (latest_runs, last_passed, latest_run_id,
latest_stage_runs, run_count, waivers, components, ha_mode) sees only rows after that
mark -- the tiles, gates, CSVs and the archive start over. Nothing is deleted: the
`*_all` reads (the process report, a history view) see every pass. An order.db made
before passes existed is pass 1 from its first run (init migrates it)."""
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
CREATE TABLE IF NOT EXISTS component(
  id INTEGER PRIMARY KEY, pos TEXT NOT NULL, kind TEXT NOT NULL, model TEXT, serial TEXT,
  slot TEXT, source_run INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS component_pos ON component(pos, id);
CREATE TABLE IF NOT EXISTS link_event(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, pos TEXT NOT NULL, link TEXT NOT NULL,
  state TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS link_event_pos ON link_event(pos, link, id);
CREATE TABLE IF NOT EXISTS pass(
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, who TEXT NOT NULL, reason TEXT NOT NULL,
  run_after INTEGER NOT NULL DEFAULT 0, waiver_after INTEGER NOT NULL DEFAULT 0,
  event_after INTEGER NOT NULL DEFAULT 0, link_after INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS run_plan(
  id INTEGER PRIMARY KEY, pass INTEGER NOT NULL, started_at TEXT NOT NULL, who TEXT NOT NULL,
  options TEXT NOT NULL, state TEXT NOT NULL, stage TEXT, held TEXT, out TEXT,
  run_after INTEGER NOT NULL, finished_at TEXT);
"""
# the current pass's marks: its rows are the runs / waivers with a higher id
_RUN_AFTER = "COALESCE((SELECT run_after FROM pass ORDER BY id DESC LIMIT 1), 0)"
_WAIVER_AFTER = "COALESCE((SELECT waiver_after FROM pass ORDER BY id DESC LIMIT 1), 0)"
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
            if not c.execute("SELECT 1 FROM pass LIMIT 1").fetchone():
                # pass 1 = everything so far (an order.db from before passes: its first run)
                first = c.execute("SELECT MIN(started_at) t FROM run").fetchone()["t"]
                c.execute("INSERT INTO pass(id,started_at,who,reason) VALUES(1,?,?,?)",
                          (first or _now(), "order", "first pass"))

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

    # ------------------------------------------------------------ passes
    def current_pass(self) -> dict:
        """{id, started_at, who, reason, run_after, waiver_after, event_after, link_after}
        of the pass in progress (its rows: ids above those marks)."""
        return dict(self._all("SELECT * FROM pass ORDER BY id DESC LIMIT 1")[0])

    def passes(self) -> list[dict]:
        """Every pass, oldest first."""
        return [dict(r) for r in self._all("SELECT * FROM pass ORDER BY id")]

    def start_pass(self, who: str, reason: str) -> dict:
        """Start fresh: a new pass from here (the runs and waivers so far leave every
        latest read; nothing is deleted). -> the new pass."""
        with closing(self._conn()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO pass(started_at,who,reason,run_after,waiver_after,"
                      "event_after,link_after) VALUES(?,?,?,"
                      + ",".join(f"(SELECT COALESCE(MAX(id),0) FROM {t})"
                                 for t in ("run", "waiver", "event", "link_event")) + ")",
                      (_now(), who, reason))
        return self.current_pass()

    # ------------------------------------------------------------ runs (current pass)
    def latest_runs(self) -> dict:
        rows = self._all(
            "SELECT * FROM run WHERE id IN (SELECT MAX(id) FROM run "
            f"WHERE id > {_RUN_AFTER} GROUP BY pos, rung)")
        return {(r["pos"], r["rung"]): {
            "stage": r["stage"], "status": r["status"], "verdict": r["verdict"],
            "detail": json.loads(r["detail"]) if r["detail"] else None,
            "artifacts": json.loads(r["artifacts"]) if r["artifacts"] else {},
            "started_at": r["started_at"], "finished_at": r["finished_at"]} for r in rows}

    def last_passed(self, rungs, *, all_passes=False) -> dict:
        """{(pos, rung): detail} of the latest PASSING run of each of `rungs` (what was
        last seen, e.g. an IOM's MAC/serial while it is silent now). all_passes=True
        looks across every pass: a fact about the hardware (a switch's lease, a MAC)
        is not a verdict, and Start fresh does not hide it."""
        rungs = list(rungs)
        scope = "" if all_passes else f"id > {_RUN_AFTER} AND "
        rows = self._all(
            f"SELECT pos, rung, detail FROM run WHERE id IN (SELECT MAX(id) FROM run WHERE {scope}"
            f"verdict='pass' AND rung IN ({','.join('?' * len(rungs))}) GROUP BY pos, rung)",
            rungs)
        return {(r["pos"], r["rung"]): json.loads(r["detail"]) if r["detail"] else {}
                for r in rows}

    def iom_sides(self) -> dict[str, str]:
        """{IOM MAC (12 hex lower): the side it last reported} from Discover's IOM serial
        reads in ANY pass and any verdict (a side is a fact about the IOM: a read that
        failed `wrong` still says which IOM is on that cable)."""
        out: dict[str, str] = {}
        for r in self._all("SELECT detail FROM run WHERE rung='serial' AND pos LIKE '%.iom%' "
                           "AND detail LIKE '%\"side\"%' ORDER BY id"):
            det = json.loads(r["detail"] or "{}")
            mac = (det.get("iom_mac") or "").replace(":", "").lower()
            if mac and det.get("side"):
                out[mac] = det["side"]
        return out

    def ever_passed(self, rungs, after: int = 0) -> set[str]:
        """The positions with a passed run of any of `rungs` in ANY pass (e.g. an install:
        the disk was written, whatever this pass has judged so far); only runs with an id
        above `after` when given (e.g. since a Run started)."""
        rungs = list(rungs)
        return {r["pos"] for r in self._all(
            "SELECT DISTINCT pos FROM run WHERE verdict='pass' AND id > ? AND rung IN "
            f"({','.join('?' * len(rungs))})", [after, *rungs])}

    def latest_run_id(self, pos, rung) -> int | None:
        """The id of (pos, rung)'s latest run row (the one latest_runs() shows)."""
        r = self._all(f"SELECT MAX(id) id FROM run WHERE pos=? AND rung=? AND id > {_RUN_AFTER}",
                      (pos, rung))
        return r[0]["id"]

    def latest_stage_runs(self) -> list[dict]:
        """The latest run per (pos, stage, rung): [{pos, stage, rung, status, verdict,
        finished_at, artifacts {name: order-dir-relative path}}], by pos, stage, rung."""
        rows = self._all(
            "SELECT * FROM run WHERE id IN (SELECT MAX(id) FROM run "
            f"WHERE id > {_RUN_AFTER} GROUP BY pos, stage, rung)"
            " ORDER BY pos, stage, rung")
        return [{"pos": r["pos"], "stage": r["stage"], "rung": r["rung"],
                 "status": r["status"], "verdict": r["verdict"],
                 "finished_at": r["finished_at"],
                 "artifacts": json.loads(r["artifacts"]) if r["artifacts"] else {}}
                for r in rows]

    def run_count(self, pos, rung) -> int:
        return self._all(f"SELECT COUNT(*) n FROM run WHERE pos=? AND rung=? AND id > {_RUN_AFTER}",
                         (pos, rung))[0]["n"]

    def runs_all(self) -> list[dict]:
        """Every run row of every pass, oldest first (the process report)."""
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None,
                 "artifacts": json.loads(r["artifacts"]) if r["artifacts"] else {}}
                for r in self._all("SELECT * FROM run ORDER BY id")]

    def mark_interrupted(self) -> int:
        # an interrupted run keeps its row; a new start_run appends a fresh one
        return self._exec(
            "UPDATE run SET status='interrupted', finished_at=? WHERE status='running'",
            (_now(),)).rowcount

    # ------------------------------------------------------------ Run (run_plan)
    # One row per server-driven Run (flax_rack.run): state running | held | interrupted |
    # cancelled | done | failed | superseded; stage = the stage it is in (or was in);
    # held = {pos: [blocker, ...]} at the hold; out = {pos: why} left out of its Deploy;
    # run_after = the highest run id when it started (what it ran since).
    def start_run_plan(self, who: str, options: dict) -> int:
        """A new Run of the current pass ('running'); a held / interrupted Run of this
        pass is superseded by it."""
        with closing(self._conn()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE run_plan SET state='superseded', finished_at=? "
                      "WHERE state IN ('running','held','interrupted')", (_now(),))
            return c.execute(
                "INSERT INTO run_plan(pass,started_at,who,options,state,run_after) "
                "VALUES((SELECT MAX(id) FROM pass),?,?,?,'running',"
                "(SELECT COALESCE(MAX(id),0) FROM run))",
                (_now(), who, json.dumps(options))).lastrowid

    def set_run_plan(self, rid: int, **kw) -> None:
        """Update a Run's state / stage / held / out (finished_at set with a final state)."""
        cols = {k: json.dumps(v) if k in ("held", "out") and v is not None else v
                for k, v in kw.items() if k in ("state", "stage", "held", "out")}
        if kw.get("state") in ("cancelled", "done", "failed", "interrupted", "superseded"):
            cols["finished_at"] = _now()
        if cols:
            self._exec(f"UPDATE run_plan SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                       (*cols.values(), rid))

    def run_plan(self, rid: int | None = None) -> dict | None:
        """Run `rid`, else the current pass's latest Run (None before one)."""
        r = (self._all("SELECT * FROM run_plan WHERE id=?", (rid,)) if rid is not None else
             self._all("SELECT * FROM run_plan WHERE pass=(SELECT MAX(id) FROM pass) "
                       "ORDER BY id DESC LIMIT 1"))
        if not r:
            return None
        x = dict(r[0])
        for k in ("options", "held", "out"):
            x[k] = json.loads(x[k]) if x[k] else {}
        return x

    def interrupt_run_plan(self) -> dict | None:
        """A 'running' Run left by a previous daemon -> 'interrupted' (its stage kept);
        returns it, None when there was none."""
        r = self._all("SELECT id FROM run_plan WHERE state='running' ORDER BY id DESC LIMIT 1")
        if not r:
            return None
        self._exec("UPDATE run_plan SET state='interrupted', finished_at=? WHERE state='running'",
                   (_now(),))
        return self.run_plan(r[0]["id"])

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

    def events_all(self) -> list[dict]:
        """Every event, oldest first (+ its id; the process report)."""
        return [dict(r) for r in self._all("SELECT id,ts,kind,text FROM event ORDER BY id")]

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
        self._set_meta_json("ha_mode", ha and {**ha, "pass": self.current_pass()["id"]})

    def ha_mode(self) -> dict | None:
        """The current pass's HA record (one from an earlier pass is not served)."""
        ha = self._meta_json("ha_mode")
        if not ha or ha.pop("pass", 1) != self.current_pass()["id"]:
            return None
        return ha

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
        """pos -> its latest waiver of this pass ({pos, stage, rungs, reason, who, at});
        every row of this pass, oldest first, with history=True."""
        rows = self._waivers(f"WHERE id > {_WAIVER_AFTER}")
        return rows if history else {r["pos"]: r for r in rows}

    def waivers_all(self) -> list[dict]:
        """Every waiver of every pass, oldest first (+ its row id)."""
        return self._waivers("")

    def _waivers(self, where) -> list[dict]:
        return [{"id": r["id"], "pos": r["pos"], "stage": r["stage"],
                 "rungs": json.loads(r["rungs"]), "reason": r["reason"], "who": r["who"],
                 "at": r["at"]}
                for r in self._all(f"SELECT * FROM waiver {where} ORDER BY id")]

    def set_closed(self, at: str | None = None) -> None:
        """Mark the order closed at `at` (UTC iso; now when None). A first close wins."""
        self._exec("INSERT OR IGNORE INTO meta(k,v) VALUES('closed_at',?)", (at or _now(),))

    def closed_at(self) -> str | None:
        r = self._all("SELECT v FROM meta WHERE k='closed_at'")
        return r[0]["v"] if r else None

    def set_components(self, pos, rows: list[dict], source_run: int) -> None:
        """Replace pos's component rows of this pass (one transaction) with `rows` ({kind,
        model, serial, slot}) read from run `source_run` (the inventory run they came
        from). An earlier pass's rows stay: the history."""
        with closing(self._conn()) as c, c:
            c.execute(f"DELETE FROM component WHERE pos=? AND source_run > {_RUN_AFTER}", (pos,))
            c.executemany(
                "INSERT INTO component(pos,kind,model,serial,slot,source_run) VALUES(?,?,?,?,?,?)",
                [(pos, r["kind"], r.get("model"), r.get("serial"), r.get("slot"), source_run)
                 for r in rows])

    def components(self, pos=None) -> list[dict]:
        """Component rows read in this pass (pos, then the order they were read in); one
        pos with pos=."""
        q = f"SELECT pos,kind,model,serial,slot,source_run FROM component WHERE source_run > {_RUN_AFTER}"
        rows = (self._all(q + " AND pos=? ORDER BY id", (pos,)) if pos is not None
                else self._all(q + " ORDER BY pos, id"))
        return [dict(r) for r in rows]

    def add_link_event(self, pos, link, state, text, at: str | None = None) -> None:
        """One live-link transition (linkmon); never a run row. Rows are never rewritten."""
        self._exec("INSERT INTO link_event(at,pos,link,state,text) VALUES(?,?,?,?,?)",
                   (at or _now(), pos, link, state, text))

    def last_link_states(self) -> dict[tuple[str, str], str]:
        """(pos, link) -> the state of its latest link_event row."""
        rows = self._all("SELECT pos,link,state FROM link_event WHERE id IN "
                         "(SELECT MAX(id) FROM link_event GROUP BY pos, link)")
        return {(r["pos"], r["link"]): r["state"] for r in rows}

    def link_events(self, limit=200) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT at,pos,link,state,text FROM link_event ORDER BY id DESC LIMIT ?", (limit,))]

    def link_events_all(self) -> list[dict]:
        """Every link transition, oldest first (the process report)."""
        return [dict(r) for r in self._all(
            "SELECT id,at,pos,link,state,text FROM link_event ORDER BY id")]
