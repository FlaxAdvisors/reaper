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
A BmcPoller thread per served rack (started by `serve`) keeps each tile's power chip,
and a LinkPoller (flax_rack.linkmon) its live switch links: /api/state carries per
device `live_links` + `presence`, per switch `live` {at, ok} + `live_links`. Tiles take
link state from those only. A `skip` run carries skip_reason: "n/a" (the rung does not
apply to the endpoint's kind: drawn green) or "upstream" (cancelled / an earlier step
failed: neutral). The HA mode is derived from the latest ha-test runs, and every row is
served in today's HA wording (bond.clean_ha: no MLAG text from older rows).

POST api/stage/deploy starts Deploy (flax_rack.deploy): rack-wide with the typed order
id, a positions run with {"confirm": true}; {"from": "firstboot"} skips the install,
{"reinstall": "<order id>"} reinstalls nodes whose install passed, {"waive": {pos:
{"rungs": [...], "reason": "..."}}} lets a device through the Qualify gate on those
rungs (recorded in order.db; who = {"by": ...} or the client address). The gate is
checked before the stage starts: a refusal is a 409 naming what blocks.

POST api/stage/done starts Done (flax_rack.done: end summary, boot reset, components,
then the customer CSVs): rack-wide with the typed order id, a positions run with
{"confirm": true}. GET api/report/devices.csv, api/report/firmware.csv and
api/report/components.csv download those CSVs (attachment; 404 until Done has written
them).

POST api/run {"confirm": <order id>, "reinstall": <order id> | false} starts a Run
(flax_rack.run: Discover -> Firmware -> Qualify -> Deploy -> Done, held before Deploy
on any blocker); reinstall is chosen up front and needs the order id typed again. POST
api/run/continue {"confirm": true, "waive": {...}} continues a held / interrupted Run
(Deploy with the gate at that click, then Done). POST api/stage/cancel stops a running
Run, and ends a held / interrupted one. /api/state carries `run` (the current pass's
latest Run: state, stage n/of, held blockers, devices left out of its Deploy).

POST api/fresh {"confirm": <order id>, "reason": "..."} starts a new pass (Start fresh:
the runs, waivers and CSVs so far leave the tiles, gates and reports; nothing is
deleted), refused like a stage start while one runs or the daemon drains. GET
api/report/process.html (re)writes and serves the internal process report (every
attempt of every pass; Close writes it too, before archiving, and a closed order's GET
serves that copy unchanged).

Serial consoles come from the shared flax-post-sol relay (flax_rungs.sol): GET
/rackpost/api/sol-targets lists every open served rack's host BMCs in the relay's feed
shape ({slots: [{port, bmc_mac, bmc_ip, bmc_pinged}]}, plus racks: [{prefix, state}]
so the relay ends a session only for a rack reported closed/disabled; a rack whose
geometry stops parsing keeps its last good targets; no credentials: the relay reads
/etc/flax/credentials-bmc.json itself, as rackpost does), and GET
api/console/<pos>?offset=&ino= tails the relay's capture file of that device's BMC for
the drawer's live viewer. The drawer's SOL button attaches the Post UI's terminal
(sol.js over the relay's /socket.io/, which nginx already proxies on this vhost).

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
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flax_rungs import sol
from flax_rungs.bond import clean_ha
from flax_rungs.pxeboot import PXE_DIR

from . import devview, process_report, report
from .addressing import plan_addresses
from .deploy import (_REASON_MAX, CONSOLE as INSTALL_CONSOLE, FROM_PHASES, PHASES as DEPLOY_PHASES,
                     GateError, blank_positions, clean_who, deploy_rungs,
                     deploy_why, gate_blockers, gate_why, ha_mode_of, installed_positions,
                     plan as deploy_plan, run_deploy, sweep_interrupted_installs)
from .done import DONE_RUNGS, done_rungs, run as run_done, targets as done_targets
from .discover_ladder import DISCOVER_STEPS, OLD_DISCOVER_RUNGS, wanted as stage_positions
from .firmware import FIRMWARE_RUNGS, run_firmware
from .geometry import Geometry, load_geometry
from .kea_client import KeaError
from . import linkmon
from .linkmon import LinkPoller
from .orderdb import OrderDB
from . import run as runmod
from .poller import INTERVAL_S, BmcPoller
from .qualify import (AGENT_STAGES, OLD_RUNG_NAMES, PHASE_STAGES, PHASES as QUALIFY_PHASES,
                      device_rungs, pop_default, qualify_passed, qualify_why, run_qualify)
from .stages import close_order, run_discover, start_fresh
from .version import BUILD, __version__

# The apply_rackpost role creates this before it copies code and restarts the daemon,
# so no stage (or Close) starts in the window between its idle check and the restart;
# a new daemon removes it at start, the role again after the restart.
DRAIN_FILE = Path(os.environ.get("RACKPOST_DRAIN_FILE", "/run/flax-rackpost.drain"))
DRAINING = "rackpost is being updated (daemon restart pending); try again in a minute"

PHASES = ("discover", "firmware", "qualify", "deploy", "done")
DISCOVER_RUNGS = DISCOVER_STEPS            # Post's 16 steps; OLD_DISCOVER_RUNGS from history
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

    def held_elsewhere(self) -> bool:
        """Another process (a CLI stage or Run) holds the flock: a non-blocking probe,
        released at once. False while this process holds it."""
        if self._t.locked():
            return False
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def busy(self) -> bool:
        """A stage runs for this rack: in this daemon or in a CLI process."""
        return self._t.locked() or self.held_elsewhere()


class Rack:
    def __init__(self, rack_dir: Path, g: Geometry, mtime=None, pxe_dir: str = PXE_DIR):
        self.dir = rack_dir
        self.g = g
        self.db = OrderDB(rack_dir / "order.db")
        self.lock = RackLock(rack_dir)
        self.stage = None                        # name of the stage this daemon runs
        self.cancel = threading.Event()          # Cancel pressed for that stage
        self.mtime = mtime
        self.linkmon = None                      # the rack's LinkPoller (set by the App)
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
                # a Run is never resumed by a restart: the operator Continues (or reruns)
                rp = self.db.interrupt_run_plan()
                if rp:
                    self.db.add_event("restart", f"run {rp['id']} interrupted by a daemon "
                                                 f"restart in {rp['stage'] or 'its start'}; "
                                                 "not resumed (Continue to Deploy, or a new Run)")
                # an install cut short leaves its hostname file: a power-on would reinstall
                sweep_interrupted_installs(self.db, g, pxe_dir=pxe_dir)
            finally:
                self.lock.release()

    def adopt(self, g: Geometry, mtime) -> None:
        """Take a geometry parsed from the file at `mtime` (a stage already running
        keeps the Geometry it started with)."""
        self.g, self.mtime = g, mtime
        self.db.init(g)


# a skip because the rung does not apply to this endpoint's kind (no host / BMC / boot /
# reservation / reader, or the node was already up -- live image or installed OS): "n/a",
# drawn green. Any other skip
# (cancelled, an earlier step or phase failed or was not measured) is "upstream": neutral.
# The producer says so (detail "na", set where the rung decides it does not apply);
# _NA_SKIP reads only rows written before that flag existed (2026-10-30).
_NA_SKIP = re.compile(r"(an IOM has no host|no BMC \(|no firmware reader for kind|"
                      r"switch endpoints are discovered|a switch has no |"
                      r"already in the live image|up on its installed OS|installed disk kept)")


def _skip_reason(run: dict) -> str:
    d = run.get("detail") or {}
    if "na" in d:
        return "n/a" if d["na"] else "upstream"
    return "n/a" if _NA_SKIP.match(d.get("reason") or d.get("headline") or "") else "upstream"


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
    if r.get("verdict") == "skip":
        r["skip_reason"] = _skip_reason(r)
    return r


# rows the drawer's step lists and the tile bars do not show: agent-stages is Qualify's
# watchdog (its failure is carried by the stage the agent was in), the SOL captures are
# the drawer's console panel (console_view). They still gate as before, and a failed one
# still fails the device (the UI counts it: hidden_rungs).
HIDDEN_RUNGS = {"qualify": ("agent-stages", "console"), "deploy": (INSTALL_CONSOLE,)}


def _shown(rungs, phase) -> list[str]:
    return [r for r in rungs if r not in HIDDEN_RUNGS[phase]]


def console_view(runs: dict) -> dict | None:
    """The device's latest SOL capture (Qualify's console or Deploy's install-console,
    whichever started last): live while its row runs; after, the scan's findings (an
    install-console scan; a Qualify capture is not scanned)."""
    have = [(r, runs[r]) for r in ("console", INSTALL_CONSOLE) if runs.get(r)]
    if not have:
        return None
    # a tie (same second): Deploy's, which comes after Qualify's
    rung, run = max(have, key=lambda x: (x[1].get("started_at") or "", x[0] == INSTALL_CONSOLE))
    d = run.get("detail") or {}
    live = run.get("status") == "running"
    findings = [r for r in d.get("rows") or [] if str(r[0]).startswith("line ")]
    if live:
        word = "live"
    elif run.get("status") != "done":
        word = run.get("status") or "?"
    elif d.get("findings"):
        word = f"{d['findings']} finding{'s' if d['findings'] != 1 else ''}"
    elif run.get("verdict") == "pass":
        word = "clean" if "findings" in d else "captured"
    elif run.get("verdict") == "skip":
        word = "not captured"
    else:
        word = "not measured" if run.get("verdict") == "unmeasured" else "failed"
    return {"rung": rung, "live": live, "word": word, "verdict": run.get("verdict"),
            "bad": run.get("verdict") in ("fail", "unmeasured") and not live,
            "headline": d.get("headline") or d.get("error") or d.get("reason") or "",
            "findings": findings, "artifacts": run.get("artifacts") or {},
            "at": run.get("finished_at") or run.get("started_at")}


def _ep_view(runs: dict, n: int, dbdev: dict, last: dict, want_side=None) -> dict:
    """One IOM: its MAC and serial from this Discover, else from the last one it passed
    (a silent IOM keeps what was seen; blank only if never seen); the side it reported
    (check_pal_sku.sh) and the side the geometry wants on its link."""
    macs = dbdev.get("iom_macs") or []
    seen = ((runs.get("bmc-mac-seen") or {}).get("detail") or {}).get("iom_mac") \
        or (last.get("bmc-mac-seen") or {}).get("iom_mac")
    ident = devview.identity(runs)
    return {"mac": _fmt_mac(macs[n - 1] if n <= len(macs) else seen),
            "serial": (ident or {}).get("serial") or (last.get("serial") or {}).get("serial"),
            "identity": ident, "fw": devview.firmware(runs),
            "side": (((runs.get("serial") or {}).get("detail") or {}).get("side")
                     or (last.get("serial") or {}).get("side")),
            "want_side": want_side}


def rack_state(r: Rack) -> dict:
    g, db = r.g, r.db
    by_pos: dict[str, dict] = {}
    raw = db.latest_runs()                  # untrimmed: the switch port tables read fdb/lldp
    # the steps drawn: a stage re-run for a device shows only its new attempt's rows (the
    # rest read not run until reached); the gates below read every latest row
    for (pos, rung), v in db.latest_runs(attempt=True).items():
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
    profiles = set(PROFILES)
    unexpected = ((by_pos.get("_rack", {}).get("slot-match") or {}).get("detail") or {}) \
        .get("unexpected") or []
    waivers = db.waivers()
    last: dict[str, dict] = {}
    # last seen in ANY pass: a MAC / serial is a fact about the hardware, not a verdict
    for (k, rung), det in db.last_passed(("bmc-mac-seen", "serial", "host-mac-seen",
                                          "host-leased", "bmc-leased"), all_passes=True).items():
        last.setdefault(k, {})[rung] = det
    inst = installed_positions(db, raw)
    lm = getattr(r, "linkmon", None)
    now = time.time()
    live = linkmon.evaluate(g, lm.snapshots() if lm else {}, linkmon.device_macs(db),
                            linkmon.device_names(g), now, db.iom_sides())
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
            # a JBOD's IOMs walk their own Discover ladders (key <pos>.iomN)
            "endpoint_runs": {f"iom{n}": by_pos.get(f"{d.pos}.iom{n}", {})
                              for n in range(1, d.endpoints.get("ioms", 0) + 1)},
            # each IOM is its own BMC endpoint: fruid-util identity + fw-util versions
            "ioms": {f"iom{n}": _ep_view(by_pos.get(f"{d.pos}.iom{n}", {}), n, x,
                                         last.get(f"{d.pos}.iom{n}", {}),
                                         d.sides.get(f"iom{n}"))
                     for n in range(1, d.endpoints.get("ioms", 0) + 1)},
            "serial": (devview.identity(runs) or {}).get("serial"),
            "identity": devview.identity(runs), "fw": devview.firmware(runs),
            "pop": ovr or default, "pop_default": default, "pop_overridden": bool(ovr),
            "host_mac": _fmt_mac(x.get("host_mac")), "bmc_mac": _fmt_mac(x.get("bmc_mac")),
            "iom_macs": [_fmt_mac(m) for m in x.get("iom_macs") or []],
            "addrs": addrs.get(d.pos, {}), "names": names.get(d.pos, {}),
            "bmc_live": x.get("bmc_live") or {"reachable": None, "power": None, "at": None},
            "live_links": live.get(d.pos, {}), "presence": linkmon.presence(live.get(d.pos, {})),
            "qualify_rungs": _shown(device_rungs(d), "qualify"),
            "console": console_view(runs),
            # Deploy: its rungs, the gate (None = may enter), the Qualify rungs a waiver
            # would have to name, and its latest recorded waiver (active = still needed)
            "deploy_rungs": _shown(deploy_rungs(d), "deploy"),
            # an installed OS on its disk (any pass): Deploy's reinstall guard
            "installed": d.pos in inst,
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
        _switch_facts(sw, last.get(f"_sw-{s.id}", {}))
        sw["live"] = lm.switch_live(s.id, now) if lm else {"at": None, "ok": False, "error": None}
        sw["live_links"] = live.get(f"_sw-{s.id}", {})
        switches.append(sw)
    ha = ha_mode_of(raw, g)                 # from the latest runs, never a stale record
    if ha:
        ha["at"] = (db.ha_mode() or {}).get("at")
    # rows written before 2026-10-30 carry the old HA wording: served in today's
    return clean_ha({
        "order": g.order, "customer": g.customer, "vid": g.vid, "site": g.site, "rack": g.rack,
        "prefix": g.url_prefix, "version": __version__, "build": BUILD, "closed_at": db.closed_at(),
        "iperf_server": g.iperf_server, "pass": _pass_view(db), "run": _run_view(r, raw),
        "stage_running": r.lock.busy(), "stage": r.stage if r.lock.locked() else None,
        "cancelling": bool(r.stage and r.cancel.is_set()),
        "phases": list(PHASES), "discover_rungs": list(DISCOVER_RUNGS),
        "discover_old_rungs": list(OLD_DISCOVER_RUNGS),
        "firmware_rungs": list(FIRMWARE_RUNGS), "firmware_old_rungs": list(FIRMWARE_OLD_RUNGS),
        "agent_stages": list(AGENT_STAGES),
        "hidden_rungs": {k: list(v) for k, v in HIDDEN_RUNGS.items()},
        "qualify_phases": list(QUALIFY_PHASES),
        "qualify_phase_stages": {k: list(v) for k, v in PHASE_STAGES.items()},
        "qualify": {"passed": passed, "not_passed": not_passed,
                    "why": qualify_why(raw, g), "progress": db.qualify_progress()},
        "deploy_phases": list(DEPLOY_PHASES), "deploy_from": list(FROM_PHASES),
        "deploy": {"passed": not dwhy,
                   "not_passed": [d.pos for d in g.devices if d.pos in dwhy],
                   "why": dwhy, "progress": db.deploy_progress(), "ha_mode": ha,
                   "waivers": db.waivers(history=True)},
        "done_rungs": list(DONE_RUNGS), "done": {"csv_at": _csv_at(r.dir)},
        "poll_interval_s": INTERVAL_S,
        "link_interval_s": lm.interval_s if lm else linkmon.INTERVAL_S, "profiles": sorted(profiles),
        "switches": switches, "devices": devices, "rack_runs": rack_runs,
        "unexpected": [{"port": u["port"], "mac": _fmt_mac(u["mac"])} for u in unexpected],
        "events": db.events(30)})


def _switch_facts(sw: dict, last: dict) -> None:
    """A switch's serial and mgmt / BMC MAC+IP are facts about the hardware: blank in
    this pass (Start fresh, or a read that failed) they show what was last seen."""
    sw["serial"] = sw.get("serial") or (last.get("serial") or {}).get("serial")
    for role, mac_step, lease_step in (("sonic", "host-mac-seen", "host-leased"),
                                       ("bmc", "bmc-mac-seen", "bmc-leased")):
        ep = sw.get(role)
        if ep is None:
            continue
        ep["mac"] = ep.get("mac") or devview._fmt((last.get(mac_step) or {}).get("mac"))
        ep["ip"] = ep.get("ip") or (last.get(lease_step) or {}).get("ip")


def _run_view(r: Rack, lr: dict | None = None) -> dict | None:
    """The current pass's latest Run for the strip ('running' with no lock held here or
    by a CLI = its process died: shown interrupted, as the next daemon start records
    it). `firmware`: the Firmware findings now (report only: they never hold the Run).
    A held / interrupted Run also carries the gate NOW (run.deploy_choice): `blocked_now`
    {key: [why]}, `waivable` {pos: [rung]}, `requalify` [pos] -- `held` is the hold's
    copy."""
    x = r.db.run_plan()
    if x is None:
        return None
    lr = r.db.latest_runs() if lr is None else lr
    state = x["state"]
    if state == "running" and not r.lock.busy():
        state = "interrupted"
    st = x["stage"]
    now = {}
    if state in runmod.CONTINUABLE:
        try:
            ch = runmod.deploy_choice(r.db, r.g, x)
            now = {k: ch[k] for k in ("blockers", "waivable", "requalify")}
        except Exception as e:                  # noqa: BLE001 -- the strip still shows the hold
            now = {"error": f"{type(e).__name__}: {e}"}
        now["blocked_now"] = now.pop("blockers", None)
    return {"id": x["id"], "state": state, "stage": st,
            "n": runmod.STAGES.index(st) + 1 if st in runmod.STAGES else 0,
            "of": len(runmod.STAGES), "reinstall": bool(x["options"].get("reinstall")),
            "who": x["who"], "started_at": x["started_at"], "finished_at": x["finished_at"],
            "held": x["held"], "out": x["out"], "can_continue": state in runmod.CONTINUABLE,
            # left out of its Deploy, deployed since (a drawer Deploy): resolved
            "out_resolved": sorted(set(x["out"]) - set(deploy_why(lr, r.g))) if x["out"] else [],
            **now,
            "firmware": runmod.firmware_findings(lr,
                [d.pos for d in r.g.devices] + runmod._sw_keys(r.g, firmware=True))}


def _pass_view(db) -> dict:
    p = db.current_pass()
    return {k: p[k] for k in ("id", "started_at", "who", "reason")}


def _csv_at(rack_dir: Path) -> str | None:
    """When Done last wrote the customer CSVs (UTC, seconds), None before."""
    try:
        t = (Path(rack_dir) / report.DEVICES_CSV).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


REPORTS = {"devices.csv": report.DEVICES_CSV, "firmware.csv": report.FIRMWARE_CSV,
           "components.csv": report.COMPONENTS_CSV}


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
        self._linkmons: dict[Path, LinkPoller] = {}
        self.link_interval_s = linkmon.INTERVAL_S

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
                ctx.links = r.linkmon               # Deploy's ha-config reads live LLDP
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
        if stage is None and not r.lock.busy():
            rp = r.db.run_plan()
            if rp and rp["state"] in (*runmod.CONTINUABLE, "running"):    # 'running': died
                r.db.set_run_plan(rp["id"], state="cancelled")
                r.db.add_event(runmod.STAGE, f"run {rp['id']} cancelled ({rp['state']})")
                return 202, {"cancelled_run": rp["id"]}
        if stage is None:
            return 409, {"error": "a CLI run holds this rack: stop it there" if r.lock.locked()
                         else "no stage is running"}
        rp = r.db.run_plan()
        in_run = bool(rp and rp["state"] == "running")
        # the operator confirmed another stage ("run" = the Run, whatever its inner stage)
        if body.get("stage") not in (None, stage) and not (body.get("stage") == runmod.STAGE
                                                           and in_run):
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
            lr = r.db.latest_runs()
            deploy_plan(lr, r.g, positions=kw.get("positions"), from_phase=frm,
                        reinstall=rein, waive=waive, installed=installed_positions(r.db, lr),
                        blank=blank_positions(r.dir, lr, [d.pos for d in r.g.devices]))
        except GateError as e:
            return 409, {"error": str(e)}
        kw.update(from_phase=frm, reinstall=rein, waive=waive, who=clean_who(by or who))
        code, out = self._start_stage(r, "deploy", run_deploy, **kw)
        if code == 202:
            out = {"started": "deploy", "positions": kw.get("positions"), "from": frm,
                   "reinstall": rein is not None, "waived": sorted((waive or {}))}
        return code, out

    def start_run(self, r: Rack, body: dict, who: str = "?"):
        """A Run: the typed order id; a reinstall (wipes installed disks) is chosen up
        front with the order id typed again."""
        if body.get("confirm") != r.g.order:
            return 400, {"error": "type the order id to confirm a Run"}
        rein = body.get("reinstall")
        if rein not in (None, False) and rein != r.g.order:
            return 400, {"error": "reinstall must be the order id (typed), or false"}
        by = body.get("by")
        if by is not None and not isinstance(by, str):
            return 400, {"error": "by must be a name"}
        who = clean_who(by or who)

        def go(ctx, **_):
            runmod.run(ctx, who=who, reinstall=bool(rein),
                       on_stage=lambda st: setattr(r, "stage", st))
        code, out = self._start_stage(r, runmod.STAGE, go)
        if code == 202:
            out = {"started": "run", "reinstall": bool(rein)}
        return code, out

    def continue_run(self, r: Rack, body: dict, who: str = "?"):
        """Continue a held / interrupted Run (run.cont): an interrupted one re-runs the
        stage it was in; a held one qualifies its not-yet-qualified hosts, then Deploy
        (the gate at this click, with the waivers sent now) -> Done."""
        if body.get("confirm") is not True:
            return 400, {"error": "confirm the continue"}
        by = body.get("by")
        if by is not None and not isinstance(by, str):
            return 400, {"error": "by must be a name"}
        if r.db.closed_at():
            return 409, {"error": "order is closed"}
        rp = r.db.run_plan()
        if rp and rp["state"] == "running":
            if r.lock.held_elsewhere():
                return 409, {"error": "a CLI run holds this rack: it is still running"}
            if not r.lock.locked():
                rp = r.db.interrupt_run_plan() or rp        # its process died
        if not rp or rp["state"] not in runmod.CONTINUABLE:
            return 409, {"error": "no held or interrupted Run to continue"}
        waive = body.get("waive")
        try:
            ch = runmod.deploy_choice(r.db, r.g, rp, waive)
        except GateError as e:
            return 400, {"error": str(e)}
        resume = rp["state"] == "interrupted" and rp["stage"] in ("discover", "firmware", "qualify")
        if not resume and rp["stage"] != "done" and not ch["targets"] and not ch["requalify"]:
            return 409, {"error": "no device passes the Deploy gate: "
                                  + "; ".join(f"{p} ({y})" for p, y in ch["out"].items())}
        who = clean_who(by or who)

        def go(ctx, **_):
            runmod.cont(ctx, rp["id"], waive=waive, who=who,
                        on_stage=lambda st: setattr(r, "stage", st))
        code, out = self._start_stage(r, runmod.STAGE, go)
        if code == 202:
            out = {"continued": rp["id"], "deploy": ch["targets"], "out": ch["out"],
                   "firstboot": ch["firstboot"], "requalify": ch["requalify"],
                   "resume": rp["stage"] if resume else None}
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

    def fresh(self, r: Rack, body: dict, who: str = "?"):
        """Start fresh: a new pass (typed order id + a reason). Refused while a stage
        runs (409, like a stage start), while draining, or once closed."""
        if body.get("confirm") != r.g.order:
            return 400, {"error": "type the order id to start fresh"}
        reason = body.get("reason")
        if not (isinstance(reason, str) and reason.strip()):
            return 400, {"error": "a reason is required"}
        if len(reason) > _REASON_MAX:
            return 400, {"error": f"reason longer than {_REASON_MAX} characters"}
        by = body.get("by")
        if by is not None and not isinstance(by, str):
            return 400, {"error": "by must be a name"}
        if r.db.closed_at():
            return 409, {"error": "order is closed"}
        if DRAIN_FILE.exists():
            return 409, {"error": DRAINING}
        if not r.lock.acquire():
            return 409, {"error": f"a stage ({r.stage or 'cli'}) is running for this rack"}
        try:
            out = start_fresh(r.dir, r.db, clean_who(by or who), reason.strip())
        finally:
            r.lock.release()
        return 200, {"pass": _pass_view(r.db), "renamed": out["renamed"]}

    def process_report(self, r: Rack) -> bytes:
        """Rewrite the process report now (a closed order: the one Close wrote, served
        as is); its HTML with links resolved from the rack root (the file links
        artifacts relative to the order dir)."""
        stored = r.dir / process_report.HTML
        if r.db.closed_at() and stored.is_file():
            text = stored.read_text()
        else:
            text = process_report.write(r.dir, r.g, r.db).read_text()
        return text.replace("<head>", '<head><base href="../../">', 1).encode()

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
            for p in [*self._pollers.values(), *self._linkmons.values()]:
                p.stop()

        t = threading.Thread(target=supervise, name="bmc-poller-supervisor", daemon=True)
        t.start()
        return t

    def sync_pollers(self, interval_s=INTERVAL_S) -> None:
        s = self.rescan()
        served = set(s.racks.values())
        for polls in (self._pollers, self._linkmons):
            for d, p in list(polls.items()):
                if d not in served:
                    p.stop()
                    del polls[d]
        for prefix, d in s.racks.items():
            if d in self._pollers and d in self._linkmons:
                continue
            r = self.rack(prefix)
            if r is None:
                continue
            if d not in self._pollers:
                p = BmcPoller(lambda r=r: self.factory(r.dir, r.g, r.db), interval_s,
                              name=f"bmc-poller-{prefix}")
                self._pollers[d] = p
                p.start()
            if d not in self._linkmons:
                # its own 30 s period (operator: links live within ~30 s)
                lp = LinkPoller(lambda r=r: self.factory(r.dir, r.g, r.db),
                                self.link_interval_s, name=f"link-poller-{prefix}")
                self._linkmons[d] = r.linkmon = lp
                lp.start()

    def stop_pollers(self) -> None:
        ev = getattr(self, "_poll_stop", None)
        if ev is not None:
            ev.set()
        for p in [*self._pollers.values(), *self._linkmons.values()]:
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
        return {"version": __version__, "build": BUILD, "orders": [orders[k] for k in sorted(orders)],
                "conflicts": s.conflicts}

    def sol_targets(self) -> dict:
        """Every open served rack's host BMCs for the flax-post-sol relay (its
        POST_BLADES_URL shape; no secrets), plus `racks`: [{prefix, state}] with state
        open | closed | disabled | unparsed. The relay ends a target's session ONLY for
        a rack reported closed or disabled, so a rack this daemon served whose
        geometry.json no longer parses (or now conflicts) keeps serving its last good
        targets as `unparsed`, and a missing rack ends nothing. disabled = a dir with a
        geometry.json that parses (or one this daemon served) whose .rackpost is gone or
        that is no longer there. A BMC Discover has not seen (no MAC) is left out, as
        the relay would drop it; bmc_pinged is the BMC poller's last answer."""
        s = self.rescan()
        slots, racks, seen = [], [], set()

        def targets(prefix, r):
            dev = {x["pos"]: x for x in r.db.devices()}
            for a in plan_addresses(r.g):
                x = dev.get(a.pos) or {}
                if a.role != "bmc" or not x.get("bmc_mac"):
                    continue
                slots.append({"port": f"rackpost:{prefix}:{a.pos}", "bmc_mac": x["bmc_mac"],
                              "bmc_ip": a.ip,
                              "bmc_pinged": bool((x.get("bmc_live") or {}).get("reachable"))})

        for prefix in sorted(s.racks):
            r = self.rack(prefix)
            if r is None:
                continue
            seen.add(prefix)
            if r.db.closed_at():
                racks.append({"prefix": prefix, "state": "closed"})
                continue
            racks.append({"prefix": prefix, "state": "open"})
            targets(prefix, r)
        served = set(s.racks.values())
        with self._mu:
            cached = [(d, r) for d, r in self._racks.items() if d not in served]
        for d, r in sorted(cached, key=lambda x: str(x[0])):
            prefix = r.g.url_prefix
            if prefix in seen:
                continue
            seen.add(prefix)
            if not ((d / ".rackpost").exists() and (d / "geometry.json").is_file()):
                racks.append({"prefix": prefix, "state": "disabled"})
            elif r.db.closed_at():
                racks.append({"prefix": prefix, "state": "closed"})
            else:                               # last good geometry: keep its targets
                racks.append({"prefix": prefix, "state": "unparsed"})
                targets(prefix, r)
        try:
            dirs = sorted(p for p in self.registry.root.iterdir() if p.is_dir())
        except OSError:
            dirs = []
        for d in dirs:                          # disabled before this daemon served it
            if d in served or (d / ".rackpost").exists() or not (d / "geometry.json").is_file():
                continue
            try:
                prefix = load_geometry(d / "geometry.json").url_prefix
            except Exception:                   # noqa: BLE001 -- unknown prefix: ends nothing
                continue
            if prefix not in seen:
                seen.add(prefix)
                racks.append({"prefix": prefix, "state": "disabled"})
        return {"slots": slots, "racks": racks}

    def console_tail(self, r: Rack, pos: str, query: str):
        """The relay's capture of pos's host BMC from ?offset= (same ?ino=), else its tail."""
        ip = next((a.ip for a in plan_addresses(r.g) if a.pos == pos and a.role == "bmc"), None)
        if ip is None:
            return 404, {"error": f"{pos}: no host BMC"}
        q = urllib.parse.parse_qs(query)

        def num(k):
            v = (q.get(k) or [""])[0]
            return int(v) if v.isdigit() else None
        return 200, {"pos": pos, "ip": ip, **sol.tail(ip, offset=num("offset"), ino=num("ino"))}

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
        """A file of a static dir, or of its vendor/ (the Post UI's xterm + socket.io)."""
        sub, _, leaf = name.rpartition("/")
        if sub not in ("", "vendor") or not _STATIC_RE.fullmatch(leaf) or ".." in leaf:
            return None
        for base in self.static_dirs:
            p = (base / name).resolve()
            if p.parent == (base / sub if sub else base) and p.is_file():
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
            f"<div class=\"tr\"><span class=\"live-ver mono\">rackpost {e(ix['build'])}</span>"
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
            if method == "GET" and rest == "api/sol-targets":
                return self._json(200, app.sol_targets())
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
            if rest.startswith("api/console/"):
                return self._json(*app.console_tail(r, rest[len("api/console/"):],
                                                    urllib.parse.urlsplit(self.path).query))
            if rest == "api/report/process.html":
                return self._send(200, app.process_report(r), _CTYPES[".html"])
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
        if rest in ("api/run", "api/run/continue"):
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            fn = app.start_run if rest == "api/run" else app.continue_run
            return self._json(*fn(r, body, who=f"ui {ip}"))
        if rest == "api/fresh":
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            return self._json(*app.fresh(r, body, who=f"ui {ip}"))
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
