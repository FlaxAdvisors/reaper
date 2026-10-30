"""flax-rackpost: one stdlib HTTP daemon serving every enabled rack order dir.

A dir under the orders root is served when it holds geometry.json AND .rackpost.
Its URL prefix is the geometry network.prefix without the trailing '-' (rr101-,
rr101b-). Two dirs claiming the same prefix, or the same (order, rack), are BOTH
refused and listed as conflicts on /rackpost/ -- never served by a guess. So are
two dirs on the same vid (each rack owns its 172.<vid>.0.0/16).

geometry.json is the hand-corrected authority: a request for a rack whose file
mtime differs from the one its Geometry was parsed at rescans, and the rack
adopts the freshly parsed geometry (or stops being served if it no longer
parses or now conflicts). Nothing is adopted from an older scan.

Artifacts saved by Qualify and Deploy are served read-only at
/<prefix>/artifacts/<pos>/<run>/<stage>/<name>.txt, path-checked like static files.
A BmcPoller thread per served rack (started by `serve`) keeps each tile's power chip.

POST api/stage/deploy starts Deploy (flax_rack.deploy): rack-wide with the typed order
id, a positions run with {"confirm": true}; {"from": "firstboot"} skips the install,
{"reinstall": "<order id>"} reinstalls nodes whose install passed, {"waive": {pos:
{"rungs": [...], "reason": "..."}}} lets a device through the Qualify gate on those
rungs (recorded in order.db; who = {"by": ...} or the client address). The gate is
checked before the stage starts: a refusal is a 409 naming what blocks.

POST api/stage/done starts Done (flax_rack.done: end summary, boot reset, components,
then the customer CSVs): rack-wide with the typed order id, a positions run with
{"confirm": true}. GET api/report/devices.csv and api/report/components.csv download
those CSVs (attachment; 404 until Done has written them).

Stages run in daemon threads under one lock per rack dir (RackLock: a thread lock
plus a flock on <dir>/.stage.lock, so a CLI `discover` and the daemon exclude each
other too). Different racks run in parallel. A stage's exception lands in that
order's event log; it never takes the server down.

The bang host has no FastAPI: stdlib only (http.server, json, threading, sqlite).
"""
import fcntl
import html
import json
import mimetypes
import os
import re
import sqlite3
import threading
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flax_rungs.fwsteps import FW_STEPS
from flax_rungs.pxeboot import PXE_DIR

from . import devview, report
from .addressing import plan_addresses
from .deploy import (FROM_PHASES, PHASES as DEPLOY_PHASES, GateError, clean_who, deploy_rungs,
                     deploy_why, gate_blockers, gate_why, plan as deploy_plan, run_deploy,
                     sweep_interrupted_installs)
from .done import DONE_RUNGS, done_rungs, run as run_done, targets as done_targets
from .discover_ladder import DISCOVER_STEPS, OLD_DISCOVER_RUNGS, wanted as stage_positions
from .firmware import run_firmware
from .geometry import Geometry, load_geometry
from .kea_client import KeaError
from .orderdb import OrderDB
from .poller import INTERVAL_S, BmcPoller
from .qualify import (AGENT_STAGES, OLD_RUNG_NAMES, PHASES as QUALIFY_PHASES, device_rungs,
                      pop_default, qualify_passed, qualify_why, run_qualify)
from .stages import close_order, run_discover
from .version import __version__

# The apply_rackpost role creates this before it copies code and restarts the daemon,
# so no stage (or Close) starts in the window between its idle check and the restart;
# a new daemon removes it at start, the role again after the restart.
DRAIN_FILE = Path(os.environ.get("RACKPOST_DRAIN_FILE", "/run/flax-rackpost.drain"))
DRAINING = "rackpost is being updated (daemon restart pending); try again in a minute"

PHASES = ("discover", "firmware", "qualify", "deploy", "done")
DISCOVER_RUNGS = DISCOVER_STEPS            # Post's 16 steps; OLD_DISCOVER_RUNGS from history
FIRMWARE_RUNGS = FW_STEPS                  # Post's 6 firmware steps (detect only)
# Qualify's old fw-versions read (removed; the Firmware stage covers it): its order.db
# rows stay in `runs` but no step list names them, so they are never drawn
FIRMWARE_OLD_RUNGS = ()
# v1 hand-written profile names (spec 5.4); a geometry `pop` or an override may add others
PROFILES = ("f4-compute-tp", "f4-bc-head-tp", "f4-bc-head-25g", "f4-bc-jbod", "ovh-bare-tp")
_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PREFIX_RE = re.compile(r"[a-z]{2}[0-9]{3}[a-z]?")      # the nginx location regex
_STATIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ART_SEG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_MAX_BODY = 64 * 1024

WEB = Path(__file__).resolve().parent / "web"
POST_STATIC = Path(__file__).resolve().parent.parent / "flax_post" / "web" / "static"
DEFAULT_STATIC = [WEB, POST_STATIC]


def _fmt_mac(m):
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if m else None


def _link_str(l) -> str:
    return f"{l.switch}:{l.port}" + (f"/{l.lane}" if l.lane else "")


# ---------------------------------------------------------------- registry
@dataclass
class Scan:
    racks: dict[str, Path]                       # url_prefix -> rack dir
    conflicts: list[dict]                        # {"key", "dirs", "error"}
    geoms: dict[Path, Geometry] = field(default_factory=dict)
    mtimes: dict[Path, int | None] = field(default_factory=dict)   # read BEFORE parsing


def _geo_mtime(d: Path):
    try:
        return (d / "geometry.json").stat().st_mtime_ns
    except OSError:
        return None


class Registry:
    def __init__(self, orders_root: Path):
        self.root = Path(orders_root)

    def scan(self) -> Scan:
        conflicts, found, mtimes = [], [], {}
        try:
            dirs = sorted(p for p in self.root.iterdir() if p.is_dir())
        except OSError as e:
            return Scan({}, [{"key": str(self.root), "dirs": [], "error": str(e)}])
        for d in dirs:
            if not ((d / "geometry.json").is_file() and (d / ".rackpost").exists()):
                continue
            mtimes[d] = _geo_mtime(d)
            try:
                g = load_geometry(d / "geometry.json")
                plan_addresses(g)                # a colliding plan is not servable
            except Exception as e:               # noqa: BLE001 -- any bad file is a conflict row
                conflicts.append({"key": d.name, "dirs": [str(d)],
                                  "error": f"geometry: {type(e).__name__}: {e}"})
                continue
            if not _PREFIX_RE.fullmatch(g.url_prefix):
                conflicts.append({"key": d.name, "dirs": [str(d)],
                                  "error": f"prefix {g.prefix!r} is not <2 letters><3 digits>[tag]-"})
                continue
            found.append((d, g))
        bad: set[Path] = set()
        keys = (("prefix", lambda g: g.url_prefix),
                ("order", lambda g: g.order + (f"/{g.rack}" if g.rack else "")),
                ("vid", lambda g: str(g.vid)))
        for label, keyf in keys:
            by: dict[str, list[Path]] = {}
            for d, g in found:
                by.setdefault(keyf(g), []).append(d)
            for k, ds in by.items():
                if len(ds) > 1:
                    conflicts.append({"key": f"{label} {k}", "dirs": [str(x) for x in ds],
                                      "error": "claimed by more than one dir; none served"})
                    bad.update(ds)
        ok = [(d, g) for d, g in found if d not in bad]
        return Scan({g.url_prefix: d for d, g in ok}, conflicts, {d: g for d, g in ok},
                    {d: mtimes[d] for d, _ in ok})


# ---------------------------------------------------------------- per-rack state
class RackLock:
    """Non-blocking stage lock for one rack dir: in-process lock + flock across processes."""

    def __init__(self, rack_dir: Path):
        self.path = Path(rack_dir) / ".stage.lock"
        self._t = threading.Lock()
        self._fd = None

    def acquire(self) -> bool:
        if not self._t.acquire(blocking=False):
            return False
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise
        except OSError:
            self._t.release()
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self._t.release()

    def locked(self) -> bool:
        return self._t.locked()


class Rack:
    def __init__(self, rack_dir: Path, g: Geometry, mtime=None, pxe_dir: str = PXE_DIR):
        self.dir = rack_dir
        self.g = g
        self.db = OrderDB(rack_dir / "order.db")
        self.lock = RackLock(rack_dir)
        self.stage = None                        # name of the stage this daemon runs
        self.cancel = threading.Event()          # Cancel pressed for that stage
        self.mtime = mtime
        self.db.init(g)
        # a 'running' row left by a previous daemon is interrupted -- unless another
        # process (a CLI discover) holds the stage lock right now
        if self.lock.acquire():
            try:
                n = self.db.mark_interrupted()
                self.db.set_qualify_progress(None)   # no Qualify runs: its phase is stale
                self.db.set_deploy_progress(None)    # ... nor Deploy
                if n:
                    self.db.add_event("restart", f"{n} rung(s) interrupted by a daemon restart")
                # an install cut short leaves its hostname file: a power-on would reinstall
                sweep_interrupted_installs(self.db, g, pxe_dir=pxe_dir)
            finally:
                self.lock.release()

    def adopt(self, g: Geometry, mtime) -> None:
        """Take a geometry parsed from the file at `mtime` (a stage already running
        keeps the Geometry it started with)."""
        self.g, self.mtime = g, mtime
        self.db.init(g)


def _seen_ha(name, ha) -> dict:
    """A pcieN link as Qualify's ha-path saw it on the wedges."""
    if ha is None:
        return {"state": "", "text": ""}
    if ha["status"] != "done":
        return {"state": "", "text": ha["status"]}
    if ha["verdict"] == "skip":
        return {"state": "", "text": "skipped"}
    if ha["verdict"] == "unmeasured":
        return {"state": "", "text": "not measured"}
    row = next((x for x in (ha["detail"] or {}).get("links") or [] if x.get("link") == name), None)
    if row is None:
        return {"state": "bad", "text": (ha["detail"] or {}).get("error") or "not checked"}
    if not row.get("seen"):
        return {"state": "bad", "text": "not seen on any wedge"}
    text = row["seen"] + (f" · {row['ifname']}" if row.get("ifname") else "")
    ok = (row.get("expected") or "").startswith(row["seen"] + " ")
    return {"state": "ok" if ok else "bad", "text": text}


def _seen(d, slot, mgmt_ids, ha=None) -> dict:
    """Per link: what Discover saw on the mgmt switch (its bmc-mac-seen, or a pre-2b
    slot-match row); pcieN links from Qualify's ha-path."""
    out = {}
    for name, l in d.links.items():
        if l.switch not in mgmt_ids:
            out[name] = _seen_ha(name, ha) if name.startswith("pcie") else {"state": "", "text": ""}
        elif slot is None:
            out[name] = {"state": "", "text": "not run"}
        elif slot["status"] != "done":
            out[name] = {"state": "", "text": slot["status"]}
        elif slot["verdict"] == "pass":
            det = slot["detail"] or {}
            if name.startswith("iom"):
                i = int(name[3:]) - 1
                ioms = det.get("iom_macs") or []
                text = _fmt_mac(ioms[i]) if i < len(ioms) else ""
            else:
                text = "host+bmc"
            out[name] = {"state": "ok", "text": text}
        else:
            det = slot["detail"] or {}
            out[name] = {"state": "bad",
                         "text": " ".join(x for x in (det.get("status"), det.get("note")) if x)
                         or det.get("reason") or det.get("error") or "fail"}
    return out


def _waiver_view(w, lr, d) -> dict | None:
    """A device's latest waiver; active = some rung it names still has not passed
    Qualify (else the waiver is history: the device has passed since)."""
    if not w:
        return None
    return {**w, "active": bool(set(w["rungs"]) & set(gate_blockers(lr, d)))}


def _trim(run: dict) -> dict:
    r = dict(run)
    if isinstance(r.get("detail"), dict) and ({"fdb", "lldp"} & set(r["detail"])):
        r["detail"] = {k: v for k, v in r["detail"].items() if k not in ("fdb", "lldp")}
    # only saved-file paths are links; Discover's inline switch text is not sent
    r["artifacts"] = {k: v for k, v in (r.get("artifacts") or {}).items()
                      if isinstance(v, str) and v.startswith("artifacts/")}
    return r


def _ep_view(runs: dict, n: int, dbdev: dict) -> dict:
    macs = dbdev.get("iom_macs") or []
    seen = ((runs.get("bmc-mac-seen") or {}).get("detail") or {}).get("iom_mac")
    ident = devview.identity(runs)
    return {"mac": _fmt_mac(macs[n - 1] if n <= len(macs) else seen),
            "serial": (ident or {}).get("serial"), "identity": ident,
            "fw": devview.firmware(runs)}


def rack_state(r: Rack) -> dict:
    g, db = r.g, r.db
    by_pos: dict[str, dict] = {}
    raw = db.latest_runs()                  # untrimmed: the switch port tables read fdb/lldp
    for (pos, rung), v in raw.items():
        by_pos.setdefault(pos, {})[rung] = _trim(v)
    for runs in by_pos.values():            # rows from before Post's step names
        for old, new in OLD_RUNG_NAMES.items():
            if old in runs:
                run = runs.pop(old)
                runs.setdefault(new, run)
    dbdev = {x["pos"]: x for x in db.devices()}
    addrs: dict[str, dict] = {}
    names: dict[str, dict] = {}
    for a in plan_addresses(g):
        addrs.setdefault(a.pos, {})[a.role] = a.ip
        names.setdefault(a.pos, {})[a.role] = a.name
    mgmt = {s.id for s in g.switches if s.role == "mgmt"}
    profiles = set(PROFILES)
    unexpected = ((by_pos.get("_rack", {}).get("slot-match") or {}).get("detail") or {}) \
        .get("unexpected") or []
    waivers = db.waivers()
    devices = []
    for d in g.devices:
        x = dbdev.get(d.pos, {})
        default = pop_default(g, d)
        ovr = x.get("pop_override")
        profiles.update(p for p in (default, ovr) if p)
        runs = by_pos.get(d.pos, {})
        devices.append({
            "pos": d.pos, "ou": d.ou, "h": d.h, "col": d.col, "kind": d.kind, "part": d.part,
            "links": {k: _link_str(l) for k, l in d.links.items()},
            "link_seen": _seen(d, runs.get("bmc-mac-seen") or runs.get("slot-match"), mgmt,
                               runs.get("ha-path")),
            # a JBOD's IOMs walk their own Discover ladders (key <pos>.iomN)
            "endpoint_runs": {f"iom{n}": by_pos.get(f"{d.pos}.iom{n}", {})
                              for n in range(1, d.endpoints.get("ioms", 0) + 1)},
            # each IOM is its own BMC endpoint: fruid-util identity + fw-util versions
            "ioms": {f"iom{n}": _ep_view(by_pos.get(f"{d.pos}.iom{n}", {}), n, x)
                     for n in range(1, d.endpoints.get("ioms", 0) + 1)},
            "serial": (devview.identity(runs) or {}).get("serial"),
            "identity": devview.identity(runs), "fw": devview.firmware(runs),
            "pop": ovr or default, "pop_default": default, "pop_overridden": bool(ovr),
            "host_mac": _fmt_mac(x.get("host_mac")), "bmc_mac": _fmt_mac(x.get("bmc_mac")),
            "iom_macs": [_fmt_mac(m) for m in x.get("iom_macs") or []],
            "addrs": addrs.get(d.pos, {}), "names": names.get(d.pos, {}),
            "bmc_live": x.get("bmc_live") or {"reachable": None, "power": None, "at": None},
            "qualify_rungs": device_rungs(d),
            # Deploy: its rungs, the gate (None = may enter), the Qualify rungs a waiver
            # would have to name, and its latest recorded waiver (active = still needed)
            "deploy_rungs": deploy_rungs(d),
            "deploy_gate": gate_why(raw, d) if deploy_rungs(d) else None,
            "gate_blockers": gate_blockers(raw, d) if deploy_rungs(d) else [],
            "waiver": _waiver_view(waivers.get(d.pos), raw, d),
            "done_rungs": done_rungs(d),
            "runs": runs})
    rack_runs = by_pos.get("_rack", {})
    passed, not_passed = qualify_passed(db, g)
    dwhy = deploy_why(raw, g)
    fdb = rack_runs.get("switch-fdb")
    switches = []
    for s in g.switches:
        sw = asdict(s)
        sw["fdb_count"] = ((fdb or {}).get("detail") or {}).get("count") \
            if s.role == "mgmt" and s.mgmt_ip else None
        sw["endpoints"] = s.endpoints
        sw["runs"] = by_pos.get(f"_sw-{s.id}", {})     # the switch's own Discover ladder
        sw.update(devview.switch_view(g, s, sw["runs"], raw))
        switches.append(sw)
    return {
        "order": g.order, "customer": g.customer, "vid": g.vid, "site": g.site, "rack": g.rack,
        "prefix": g.url_prefix, "version": __version__, "closed_at": db.closed_at(),
        "iperf_server": g.iperf_server,
        "stage_running": r.lock.locked(), "stage": r.stage if r.lock.locked() else None,
        "cancelling": bool(r.stage and r.cancel.is_set()),
        "phases": list(PHASES), "discover_rungs": list(DISCOVER_RUNGS),
        "discover_old_rungs": list(OLD_DISCOVER_RUNGS),
        "firmware_rungs": list(FIRMWARE_RUNGS), "firmware_old_rungs": list(FIRMWARE_OLD_RUNGS),
        "agent_stages": list(AGENT_STAGES),
        "qualify_phases": list(QUALIFY_PHASES),
        "qualify": {"passed": passed, "not_passed": not_passed,
                    "why": qualify_why(raw, g), "progress": db.qualify_progress()},
        "deploy_phases": list(DEPLOY_PHASES), "deploy_from": list(FROM_PHASES),
        "deploy": {"passed": not dwhy,
                   "not_passed": [d.pos for d in g.devices if d.pos in dwhy],
                   "why": dwhy, "progress": db.deploy_progress(), "ha_mode": db.ha_mode(),
                   "waivers": db.waivers(history=True)},
        "done_rungs": list(DONE_RUNGS), "done": {"csv_at": _csv_at(r.dir)},
        "poll_interval_s": INTERVAL_S, "profiles": sorted(profiles),
        "switches": switches, "devices": devices, "rack_runs": rack_runs,
        "unexpected": [{"port": u["port"], "mac": _fmt_mac(u["mac"])} for u in unexpected],
        "events": db.events(30)}


def _csv_at(rack_dir: Path) -> str | None:
    """When Done last wrote the customer CSVs (UTC, seconds), None before."""
    try:
        t = (Path(rack_dir) / report.DEVICES_CSV).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


REPORTS = {"devices.csv": report.DEVICES_CSV, "components.csv": report.COMPONENTS_CSV}


# ---------------------------------------------------------------- app
class App:
    def __init__(self, orders_root: Path, static_dirs, context_factory):
        self.registry = Registry(orders_root)
        self.static_dirs = [Path(p).resolve() for p in static_dirs]
        self.factory = context_factory
        self._mu = threading.Lock()
        self._scan = Scan({}, [])
        self._racks: dict[Path, Rack] = {}
        self._pollers: dict[Path, BmcPoller] = {}

    def rescan(self) -> Scan:
        s = self.registry.scan()
        with self._mu:
            self._scan = s
        return s

    def rack(self, prefix: str) -> Rack | None:
        s = self._scan
        d = s.racks.get(prefix)
        if d is None or not ((d / ".rackpost").exists() and (d / "geometry.json").is_file()) \
                or _geo_mtime(d) != s.mtimes.get(d):
            # unknown, disabled, or geometry.json edited since that scan parsed it
            s = self.rescan()
            d = s.racks.get(prefix)
            if d is None:
                return None
        with self._mu:
            r = self._racks.get(d)
            if r is None:
                r = self._racks[d] = Rack(d, s.geoms[d], s.mtimes[d])
            elif r.mtime != s.mtimes[d]:
                r.adopt(s.geoms[d], s.mtimes[d])
        return r

    # -- actions --
    def _start_stage(self, r: Rack, name: str, fn, **kw):
        if r.db.closed_at():
            return 409, {"error": "order is closed"}
        if DRAIN_FILE.exists():
            return 409, {"error": DRAINING}
        if not r.lock.acquire():
            return 409, {"error": f"a stage ({r.stage or 'cli'}) is already running for this rack"}
        r.cancel = cancel = threading.Event()    # a fresh flag: an old Cancel never carries
        r.stage = name
        g, db = r.g, r.db

        def work():
            try:
                ctx = self.factory(r.dir, g, db)
                ctx.cancel = cancel
                fn(ctx, **kw)
            except Exception as e:           # noqa: BLE001 -- logged, never fatal
                try:
                    db.add_event("error", f"{name}: {type(e).__name__}: {e}")
                except Exception:            # noqa: BLE001
                    pass
            finally:
                r.stage = None
                r.lock.release()

        try:
            if name == "discover" and "positions" not in kw:
                db.add_event("discover", "discover started")
            threading.Thread(target=work, name=f"{name}-{g.url_prefix}", daemon=True).start()
        except BaseException:
            r.stage = None
            r.lock.release()
            raise
        return 202, {"started": name, **kw}

    def _positions(self, r: Rack, pos):
        """(None, sorted positions) or (error response, None)."""
        if not (isinstance(pos, list) and pos and all(isinstance(p, str) for p in pos)):
            return (400, {"error": "positions must be a non-empty list of positions"}), None
        try:
            stage_positions(r.g, pos)
        except ValueError as e:
            return (400, {"error": str(e)}), None
        return None, sorted(set(pos))

    def start_discover(self, r: Rack, body: dict):
        # Discover may power on / cycle nodes (power-on step): Qualify's confirm rules --
        # rack-wide needs the typed order id, a positions run an explicit {"confirm": true}
        pos = body.get("positions")
        if pos is None:
            if body.get("confirm") != r.g.order:
                return 400, {"error": "type the order id to confirm a rack-wide Discover"}
            return self._start_stage(r, "discover", run_discover)
        if body.get("confirm") is not True:
            return 400, {"error": "confirm the positions to re-discover"}
        err, pos = self._positions(r, pos)
        return err or self._start_stage(r, "discover", run_discover, positions=pos)

    def start_firmware(self, r: Rack, body: dict):
        # detect only (reads versions, never flashes or powers): no confirm needed
        pos = body.get("positions")
        if pos is None:
            return self._start_stage(r, "firmware", run_firmware)
        err, pos = self._positions(r, pos)
        return err or self._start_stage(r, "firmware", run_firmware, positions=pos)

    def cancel_stage(self, r: Rack, body: dict):
        """Set this rack's cancel flag: the running stage stops between steps/phases."""
        if body.get("confirm") is not True:
            return 400, {"error": "confirm the cancel"}
        stage = r.stage
        if stage is None:
            return 409, {"error": "a CLI run holds this rack: stop it there" if r.lock.locked()
                         else "no stage is running"}
        if body.get("stage") not in (None, stage):     # the operator confirmed another stage
            return 409, {"error": f"the running stage is {stage}, not {body.get('stage')}"}
        if not r.cancel.is_set():
            r.cancel.set()
            r.db.add_event(stage, f"{stage} cancel requested")
        return 202, {"cancelling": stage}

    def start_qualify(self, r: Rack, body: dict):
        # Qualify power-cycles nodes, clears SELs and runs stress for hours: the
        # rack-wide form needs the typed order id (like Close), a positions run an
        # explicit {"confirm": true}.
        pos = body.get("positions")
        if pos is None and body.get("confirm") != r.g.order:
            return 400, {"error": "type the order id to confirm a rack-wide Qualify"}
        if pos is not None and body.get("confirm") is not True:
            return 400, {"error": "confirm the positions to re-qualify"}
        if pos is not None:
            hosts = {d.pos for d in r.g.devices if d.endpoints.get("hosts")}
            if not (isinstance(pos, list) and pos and all(isinstance(p, str) for p in pos)):
                return 400, {"error": "positions must be a non-empty list of positions"}
            unknown = sorted(set(pos) - {d.pos for d in r.g.devices})
            if unknown:
                return 400, {"error": f"unknown position: {', '.join(unknown)}"}
            # a JBOD (no host) has nothing to qualify: skip it, run the rest
            skipped = sorted(set(pos) - hosts)
            run = sorted(set(pos) & hosts)
            if not run:
                return 400, {"error": f"not a host-bearing position: {', '.join(skipped)}"}
            code, body = self._start_stage(r, "qualify", run_qualify, positions=run)
            if skipped and code == 202:
                body["skipped"] = skipped
            return code, body
        return self._start_stage(r, "qualify", run_qualify)

    def start_deploy(self, r: Rack, body: dict, who: str = "?"):
        # Deploy reinstalls nodes (wipes disks): rack-wide needs the typed order id, a
        # positions run an explicit {"confirm": true}; a node whose install already
        # passed is reinstalled only with {"reinstall": "<order id>"}. The gate (with the
        # request's waivers) is checked here so a refusal is the response, then again
        # by run_deploy itself.
        pos = body.get("positions")
        if pos is None and body.get("confirm") != r.g.order:
            return 400, {"error": "type the order id to confirm a rack-wide Deploy"}
        if pos is not None and body.get("confirm") is not True:
            return 400, {"error": "confirm the positions to deploy"}
        kw = {}
        if pos is not None:
            if not (isinstance(pos, list) and pos and all(isinstance(p, str) for p in pos)):
                return 400, {"error": "positions must be a non-empty list of positions"}
            unknown = sorted(set(pos) - {d.pos for d in r.g.devices})
            if unknown:
                return 400, {"error": f"unknown position: {', '.join(unknown)}"}
            kw["positions"] = sorted(set(pos))
        frm = body.get("from", "install")
        if frm not in FROM_PHASES:
            return 400, {"error": f"from must be one of {', '.join(FROM_PHASES)}"}
        rein = body.get("reinstall")
        if rein is not None and rein != r.g.order:
            return 400, {"error": "reinstall must be the order id"}
        waive = body.get("waive")
        by = body.get("by")
        if by is not None and not isinstance(by, str):
            return 400, {"error": "by must be a name"}
        if r.db.closed_at():
            return 409, {"error": "order is closed"}
        try:
            deploy_plan(r.db.latest_runs(), r.g, positions=kw.get("positions"), from_phase=frm,
                        reinstall=rein, waive=waive)
        except GateError as e:
            return 409, {"error": str(e)}
        kw.update(from_phase=frm, reinstall=rein, waive=waive, who=clean_who(by or who))
        code, out = self._start_stage(r, "deploy", run_deploy, **kw)
        if code == 202:
            out = {"started": "deploy", "positions": kw.get("positions"), "from": frm,
                   "reinstall": rein is not None, "waived": sorted((waive or {}))}
        return code, out

    def start_done(self, r: Rack, body: dict):
        # Done clears the boot override and the hostname iPXE file (never a power
        # action) and rewrites the customer CSVs: rack-wide = the typed order id, a
        # positions run an explicit {"confirm": true}
        pos = body.get("positions")
        if pos is None and body.get("confirm") != r.g.order:
            return 400, {"error": "type the order id to confirm a rack-wide Done"}
        if pos is None:
            return self._start_stage(r, "done", run_done)
        if body.get("confirm") is not True:
            return 400, {"error": "confirm the positions to run Done for"}
        if not (isinstance(pos, list) and pos and all(isinstance(p, str) for p in pos)):
            return 400, {"error": "positions must be a non-empty list of positions"}
        try:
            done_targets(r.g, pos)
        except ValueError as e:
            return 400, {"error": str(e)}
        return self._start_stage(r, "done", run_done, positions=sorted(set(pos)))

    def report_file(self, r: Rack, name: str) -> tuple[Path, str] | None:
        """(the CSV Done wrote, its download name) or None (unknown, not written yet)."""
        fn = REPORTS.get(name)
        if fn is None or not (r.dir / fn).is_file():
            return None
        tag = re.sub(r"[^A-Za-z0-9._-]", "_", r.g.order + (f"-{r.g.rack}" if r.g.rack else ""))
        return r.dir / fn, f"{tag}-{fn}"

    # -- BMC pollers: one per served rack; a disabled/vanished rack's poller stops --
    def start_pollers(self, interval_s=INTERVAL_S) -> threading.Thread:
        self._poll_stop = threading.Event()

        def supervise():
            while True:
                try:
                    self.sync_pollers(interval_s)
                except Exception:            # noqa: BLE001
                    pass
                if self._poll_stop.wait(interval_s):
                    break
            for p in list(self._pollers.values()):
                p.stop()

        t = threading.Thread(target=supervise, name="bmc-poller-supervisor", daemon=True)
        t.start()
        return t

    def sync_pollers(self, interval_s=INTERVAL_S) -> None:
        s = self.rescan()
        served = set(s.racks.values())
        for d, p in list(self._pollers.items()):
            if d not in served:
                p.stop()
                del self._pollers[d]
        for prefix, d in s.racks.items():
            if d in self._pollers:
                continue
            r = self.rack(prefix)
            if r is None:
                continue
            p = BmcPoller(lambda r=r: self.factory(r.dir, r.g, r.db), interval_s,
                          name=f"bmc-poller-{prefix}")
            self._pollers[d] = p
            p.start()

    def stop_pollers(self) -> None:
        ev = getattr(self, "_poll_stop", None)
        if ev is not None:
            ev.set()
        for p in list(self._pollers.values()):
            p.stop()

    def set_pop(self, r: Rack, body: dict):
        pos, profile = body.get("pos"), body.get("profile")
        try:
            d = r.g.device(pos)
        except KeyError:
            return 404, {"error": f"no device {pos!r}"}
        if profile is not None and not (isinstance(profile, str) and _PROFILE_RE.fullmatch(profile)):
            return 400, {"error": "profile must be null or a profile name"}
        if r.db.closed_at():
            return 409, {"error": "order is closed"}
        default = pop_default(r.g, d)
        if profile == default:
            profile = None
        r.db.set_pop(pos, profile)
        r.db.add_event("pop", f"{pos} pop -> {profile or 'default ' + default}")
        return 200, {"pos": pos, "pop": profile or default, "pop_overridden": bool(profile)}

    def close(self, r: Rack, body: dict):
        if body.get("confirm") != r.g.order:
            return 400, {"error": "type the order id to confirm"}
        if DRAIN_FILE.exists():
            return 409, {"error": DRAINING}
        if not r.lock.acquire():
            return 409, {"error": "a stage is running for this rack"}
        try:
            out = close_order(self.factory(r.dir, r.g, r.db))
        except KeaError as e:
            r.db.add_event("error", f"close: {e}")
            return 502, {"error": str(e)}
        except Exception as e:               # noqa: BLE001
            r.db.add_event("error", f"close: {type(e).__name__}: {e}")
            return 500, {"error": f"{type(e).__name__}: {e}"}
        finally:
            r.lock.release()
        return 200, {"closed_at": r.db.closed_at(), **out}

    # -- index --
    def index(self) -> dict:
        s = self.rescan()
        orders: dict[str, dict] = {}
        for prefix, d in sorted(s.racks.items()):
            g = s.geoms[d]
            o = orders.setdefault(g.order, {"order": g.order, "customer": g.customer, "racks": []})
            o["racks"].append({"rack": g.rack, "prefix": prefix, "url": f"/{prefix}/",
                               "vid": g.vid, "site": g.site, "dir": str(d),
                               "closed_at": _closed_at(d)})
        return {"version": __version__, "orders": [orders[k] for k in sorted(orders)],
                "conflicts": s.conflicts}

    def artifact_file(self, r: Rack, rest: str) -> Path | None:
        """artifacts/<pos>/<run>/<stage>/<name>.txt inside the order dir, nothing else."""
        segs = rest.split("/")
        if len(segs) != 5 or segs[0] != "artifacts" or not segs[4].endswith(".txt") \
                or not all(_ART_SEG_RE.fullmatch(x) and ".." not in x for x in segs[1:]):
            return None
        base = (r.dir / "artifacts").resolve()
        p = (r.dir / rest).resolve()
        try:
            p.relative_to(base)
        except ValueError:                       # a symlink out of the order dir
            return None
        return p if p.is_file() else None

    def static_file(self, name: str) -> Path | None:
        if not _STATIC_RE.fullmatch(name) or ".." in name:
            return None
        for base in self.static_dirs:
            p = (base / name).resolve()
            if p.parent == base and p.is_file():
                return p
        return None


def _closed_at(d: Path):
    p = d / "order.db"
    if not p.exists():
        return None
    try:
        return OrderDB(p).closed_at()
    except sqlite3.Error:
        return None


def index_html(ix: dict) -> str:
    e = html.escape
    rows = []
    for o in ix["orders"]:
        rows.append(f'<div class="group"><div class="group-h" style="grid-template-columns:1fr">'
                    f'<span class="col-h" style="text-align:left">{e(o["order"])} · {e(o["customer"])}'
                    f'</span></div>')
        for r in o["racks"]:
            state = f'closed {e(r["closed_at"])}' if r["closed_at"] else "open"
            rows.append(f'<div class="row full"><span class="ou-label">{e(r["rack"] or "·")}</span>'
                        f'<a class="tile sw" href="{e(r["url"])}"><div class="t-head">'
                        f'<span class="t-port">{e(r["prefix"])}</span>'
                        f'<span class="kind">vid {r["vid"]} · {e(r["site"])}</span>'
                        f'<span class="watt {"unk" if r["closed_at"] else "on"}">{state}</span>'
                        f'</div></a></div>')
        rows.append("</div>")
    if not ix["orders"]:
        rows.append('<p class="pending-note">no enabled rack orders (geometry.json + .rackpost)</p>')
    if ix["conflicts"]:
        rows.append('<div class="section-l">conflicts (not served)</div>')
        for c in ix["conflicts"]:
            rows.append(f'<div class="t-row"><span class="v">{e(c["key"])}</span>'
                        f'<span title="{e(c.get("error", ""))}">{e(", ".join(c["dirs"]))}'
                        f' · {e(c.get("error", ""))}</span></div>')
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>rackpost orders</title>"
            "<link rel=\"icon\" href=\"/rackpost/static/flax-logo.svg\" type=\"image/svg+xml\">"
            "<link rel=\"stylesheet\" href=\"/rackpost/static/rack.css\">"
            "<link rel=\"stylesheet\" href=\"/rackpost/static/rackpost.css\"></head><body>"
            "<div class=\"top\"><div class=\"toprow\"><div class=\"tl\">"
            "<img class=\"flogo\" src=\"/rackpost/static/flax-logo.svg\" alt=\"flax\"></div>"
            f"<div class=\"tr\"><span class=\"live-ver mono\">rackpost {e(ix['version'])}</span>"
            "<span class=\"title\">rack orders</span></div></div></div>"
            "<div class=\"rack index\">" + "".join(rows) + "</div></body></html>")


# ---------------------------------------------------------------- http
_CTYPES = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
           ".html": "text/html; charset=utf-8"}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"flax-rackpost/{__version__}"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # -- responses --
    def _send(self, code, body: bytes, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")   # artifacts are node-written
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _redirect(self, loc):
        self.send_response(301)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, name):
        p = self.server.app.static_file(name)
        if p is None:
            return self._json(404, {"error": "not found"})
        ctype = _CTYPES.get(p.suffix) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self._send(200, p.read_bytes(), ctype)

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 <= n <= _MAX_BODY:
            return None
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw or b"{}")
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    # -- routing --
    def _dispatch(self, method):
        try:
            self._route(method, urllib.parse.urlsplit(self.path).path)
        except Exception as e:                   # noqa: BLE001 -- a request never kills the server
            try:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:                    # noqa: BLE001
                pass

    def _route(self, method, path):
        app: App = self.server.app
        parts = path.split("/")
        if len(parts) < 2 or not parts[1]:
            return self._redirect("/rackpost/")
        head, rest = parts[1], "/".join(parts[2:])
        if len(parts) == 2:
            return self._redirect(f"/{head}/")
        if head == "rackpost":
            if method == "GET" and rest == "":
                return self._send(200, index_html(app.index()).encode(), _CTYPES[".html"])
            if method == "GET" and rest == "api/racks":
                return self._json(200, app.index())
            if method == "GET" and rest.startswith("static/"):
                return self._static(rest[len("static/"):])
            return self._json(404, {"error": "not found"})
        if not _PREFIX_RE.fullmatch(head):
            return self._json(404, {"error": "not found"})
        r = app.rack(head)
        if r is None:
            return self._json(404, {"error": f"no enabled rack order {head!r}"})
        if method == "GET":
            if rest == "":
                return self._static("index.html")
            if rest.startswith("static/"):
                return self._static(rest[len("static/"):])
            if rest == "api/state":
                return self._json(200, rack_state(r))
            if rest.startswith("api/report/"):
                f = app.report_file(r, rest[len("api/report/"):])
                if f is None:
                    return self._json(404, {"error": "not written yet (run Done)"})
                return self._send(200, f[0].read_bytes(), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{f[1]}"'})
            if rest.startswith("artifacts/"):
                p = app.artifact_file(r, rest)
                if p is None:
                    return self._json(404, {"error": "not found"})
                return self._send(200, p.read_bytes(), "text/plain; charset=utf-8")
            return self._json(404, {"error": "not found"})
        # CSRF: a cross-site form can only send a "simple" Content-Type; JSON
        # forces a preflight this server never answers
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
        body = self._body()
        if body is None:
            return self._json(400, {"error": "body must be a JSON object"})
        if rest == "api/pop":
            return self._json(*app.set_pop(r, body))
        if rest == "api/stage/discover":
            return self._json(*app.start_discover(r, body))
        if rest == "api/stage/cancel":
            return self._json(*app.cancel_stage(r, body))
        if rest == "api/stage/firmware":
            return self._json(*app.start_firmware(r, body))
        if rest == "api/stage/qualify":
            return self._json(*app.start_qualify(r, body))
        if rest == "api/stage/deploy":
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            return self._json(*app.start_deploy(r, body, who=f"ui {ip}"))
        if rest == "api/stage/done":
            return self._json(*app.start_done(r, body))
        if rest == "api/close":
            return self._json(*app.close(r, body))
        return self._json(404, {"error": "not found"})


def default_context_factory(rack_dir: Path, g: Geometry, db: OrderDB):
    from .creds import load_creds
    from .kea_client import KeaClient
    from .stages import OrderContext
    # creds load lazily (first switch/BMC rung): Close needs Kea only
    return OrderContext(rack_dir, g, db, KeaClient(), load_creds)


def make_server(orders_root: Path, *, port=8452, bind="127.0.0.1", static_dirs=None,
                context_factory=default_context_factory) -> ThreadingHTTPServer:
    DRAIN_FILE.unlink(missing_ok=True)      # this process is the update the drain waited for
    srv = ThreadingHTTPServer((bind, port), _Handler)
    srv.daemon_threads = True
    srv.app = App(Path(orders_root), static_dirs or DEFAULT_STATIC, context_factory)
    return srv
