"""Deploy stage for a rack order (spec sections 7.2a, 7.3, 7.3b), run in lockstep phases.

Five rack-wide phases, each started on every still-eligible device at once, with a
barrier (wait for all) before the next:

  install    SOL capture starts; install.install: hostname iPXE file, Redfish Pxe +   3600 s
             power cycle, the wiwynn-tp autoinstall powers the node off
  firstboot  install.firstboot: Redfish Hdd, power on, ssh as flax, installed-OS     1200 s
             proof; then the SOL capture stops and is scanned (install-console)
  user       ansible.flax_user: flax_user.yml once over every node still in         1800 s
  ha-config  bond.configure: bond0 active-backup over the two PCIe ports (by MAC),    300 s
             primary = the wedge-A port Qualify's ha-path saw, A-side fabric address
  ha-test    hatest.run once for the rack: whole-wedge cut A, then B, iperf mesh     1200 s
             over bond0 against geometry.iperf_server

The install/firstboot/user deadlines are the rungs' own (deadline_s / timeout);
ha-config and ha-test are bounded by their rungs' ssh timeouts and settle windows.

Gate: a device enters Deploy only when its latest Qualify passed (qualify.device_rungs
all passed and Post's verdict pass). Rack-wide, every host-bearing device must pass; a
`positions` run needs each listed device to pass and records "rack incomplete: ..." as an
event for the others. A recorded WAIVER (operator, per device: the named Qualify rungs,
a reason, who, when -- stored in order.db) lets a device through when every rung it has
not passed is named in it; any other failed/unmeasured rung still blocks. A waiver is
carried by the request that uses it (never inherited from an earlier Deploy).

Reinstall guard: a target whose latest `install` passed is only reinstalled (its disks
wiped) with reinstall=<order id>; else the run is refused and `from_phase="firstboot"`
(skip install: SOL, Hdd boot, installed-OS proof, then the rest) is the way on. A
firstboot run needs each target's latest install (or firstboot) passed.

A device failing a phase (fail or unmeasured on its rung) gets `skip` ("failed in phase
<p>" / "not measured in phase <p>" / "cancelled") on the later phases' rungs; the rest
carry on. install-console never gates the later phases (Qualify's console rule) but a
fail there still fails the device in deploy_passed. The rack's Cancel is checked before
each device's install and at every barrier (an install already powering the node runs
to its end). The current phase is kept in order.db (deploy_progress) for the UI.

ha-test needs the iperf server: when it is not one of this run's devices, its latest
ha-config must have passed (it joins the test: its wedge ports are cut too), else every
participant fails "server <pos> not deployed". After a test the order records the HA
mode delivered (order.db ha_mode: mode, devices whose latest ha-test passed, missing).

flax_rungs functions never touch order.db; this module records them. ctx.rungs (a
namespace like real_rungs()) replaces them in tests and in RACKPOST_FAKE.
"""
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

from flax_rungs import agent, ansible, bond, fabric, hatest, install, pxeboot, sol, solscan
from flax_rungs.result import UNMEASURED, RungResult, not_passed

from .addressing import plan_addresses
from .qualify import (AGENT_STAGES, _Progress, _artifact, _safe, _stage_ok, device_rungs,
                      device_verdict, parse_ip_link)
from .stages import cancelled, creds

STAGE = "deploy"
PHASES = ("install", "firstboot", "user", "ha-config", "ha-test")
FROM_PHASES = ("install", "firstboot")
DEADLINES = {"install": 3600, "firstboot": 1200, "user": 1800, "ha-config": 300,
             "ha-test": 1200}
PHASE_RUNG = {"install": "install", "firstboot": "firstboot", "user": "flax-user",
              "ha-config": "ha-config", "ha-test": "ha-test"}
CONSOLE = "install-console"
NON_GATING = (CONSOLE,)
MAX_PARALLEL = 8            # concurrent installs / first boots (BMC power actions)
_REASON_MAX = 500


class GateError(ValueError):
    """Deploy refused before anything ran (gate, reinstall guard, a bad waiver)."""


def real_rungs():
    return SimpleNamespace(
        install=install.install, firstboot=install.firstboot, sol_start=sol.start,
        sol_stop=sol.stop, sol_scan=solscan.scan_file, flax_user=ansible.flax_user,
        bond_config=bond.configure, ha_test=hatest.run)


def deploy_rungs(d) -> list[str]:
    """The Deploy rungs of a device ([] = no host); ha-* only with PCIe fabric links."""
    if not d.endpoints.get("hosts"):
        return []
    out = ["install", "firstboot", CONSOLE, "flax-user"]
    if any(n.startswith("pcie") for n in d.links):
        out += ["ha-config", "ha-test"]
    return out


def deploy_why(lr, g) -> dict[str, str]:
    """pos -> why it is not deployed (absent = deployed): its first Deploy rung (step
    order) whose latest run is not pass."""
    out = {}
    for d in g.devices:
        for r in deploy_rungs(d):
            v = (lr.get((d.pos, r)) or {}).get("verdict")
            if v != "pass":
                out[d.pos] = not_passed(r, v)
                break
    return out


def deploy_passed(db, g) -> tuple[bool, list[str]]:
    why = deploy_why(db.latest_runs(), g)
    bad = [d.pos for d in g.devices if d.pos in why]
    return not bad, bad


# ---------------------------------------------------------------- gate
def gate_blockers(lr, d) -> list[str]:
    """The Qualify rungs of d not passed (step order); ["verdict <v>"] when every rung
    looks passed but Post's verdict is not pass. [] = Qualify passed."""
    bad = []
    for r in device_rungs(d):
        run = lr.get((d.pos, r))
        if not (_stage_ok(run) if r in AGENT_STAGES else (run or {}).get("verdict") == "pass"):
            bad.append(r)
    if not bad and device_rungs(d):
        v = device_verdict(lr, d)
        if v != "pass":
            bad.append(f"verdict {v or 'none'}")
    return bad


def gate_why(lr, d, waived=()) -> str | None:
    """Why d may not enter Deploy (None = it may): every Qualify rung it has not passed
    and that `waived` does not name."""
    if not deploy_rungs(d):
        return "not a host-bearing device"
    left = [r for r in gate_blockers(lr, d) if r not in waived]
    if not left:
        return None
    vs = {r: (lr.get((d.pos, r)) or {}).get("verdict") for r in left}
    shown = [r for r in left if vs[r] != "skip"] or left[:1]
    skipped = len(left) - len(shown)
    return "qualify not passed: " + ", ".join(
        r if r.startswith("verdict ") else not_passed(r, vs[r]) for r in shown) + \
        (f" ({skipped} later rung(s) skipped)" if skipped else "")


def check_waivers(g, waive, targets: list[str]) -> dict[str, dict]:
    """The request's waivers, validated: {pos: {"rungs": [...], "reason": str}}. Only a
    device of this run, only its own Qualify rungs, and a reason is required."""
    if waive is None:
        return {}
    if not isinstance(waive, dict):
        raise GateError("waive must be {pos: {\"rungs\": [...], \"reason\": \"...\"}}")
    out = {}
    for pos, w in waive.items():
        if pos not in targets:
            raise GateError(f"waiver for {pos}: not a device of this Deploy")
        if not isinstance(w, dict):
            raise GateError(f"waiver for {pos}: want {{\"rungs\": [...], \"reason\": \"...\"}}")
        rungs, reason = w.get("rungs"), w.get("reason")
        if not (isinstance(rungs, list) and rungs and all(isinstance(r, str) for r in rungs)):
            raise GateError(f"waiver for {pos}: rungs must be a non-empty list of rung names")
        known = device_rungs(g.device(pos))
        unknown = [r for r in rungs if r not in known]
        if unknown:
            raise GateError(f"waiver for {pos}: not a Qualify rung of {pos}: {', '.join(unknown)}")
        if not isinstance(reason, str) or not reason.strip():
            raise GateError(f"waiver for {pos}: a reason is required")
        if len(reason) > _REASON_MAX:
            raise GateError(f"waiver for {pos}: reason longer than {_REASON_MAX} characters")
        out[pos] = {"rungs": list(dict.fromkeys(rungs)), "reason": reason.strip()}
    return out


def plan(lr, g, *, positions=None, from_phase="install", reinstall=None, waive=None):
    """(target devices, waivers, {pos: why} of the other host devices not passed). Raises
    GateError when the run may not start: unknown position, the gate (with the waivers),
    or a passed install without reinstall=<order id>."""
    if from_phase not in FROM_PHASES:
        raise GateError(f"from must be one of {', '.join(FROM_PHASES)}")
    hosts = [d for d in g.devices if d.endpoints.get("hosts")]
    if positions:
        unknown = sorted(set(positions) - {d.pos for d in hosts})
        if unknown:
            raise GateError(f"not a host-bearing position: {', '.join(unknown)}")
        targets = [d for d in hosts if d.pos in positions]
    else:
        targets = hosts
    if not targets:
        raise GateError("no host-bearing device to deploy")
    w = check_waivers(g, waive, [d.pos for d in targets])
    blocked = {d.pos: why for d in targets
               if (why := gate_why(lr, d, w.get(d.pos, {}).get("rungs", ())))}
    if blocked:
        scope = "rack-wide Deploy needs every host-bearing device passed; " if not positions else ""
        raise GateError("deploy gate: " + scope + "; ".join(f"{p} {y}" for p, y in blocked.items()))
    tset = {d.pos for d in targets}
    incomplete = {d.pos: why for d in hosts if d.pos not in tset and (why := gate_why(lr, d))}
    if from_phase == "install":
        done = [d.pos for d in targets
                if (lr.get((d.pos, "install")) or {}).get("verdict") == "pass"]
        if done and reinstall != g.order:
            raise GateError(
                f"already installed: {', '.join(done)}; a reinstall wipes their disks -- "
                f"confirm with reinstall {g.order} (CLI --reinstall {g.order}), or deploy "
                f"from firstboot (CLI --from firstboot)")
    else:
        # a first boot of a never-installed disk boots whatever is on it, then fails slowly
        none = [d.pos for d in targets
                if not any((lr.get((d.pos, r)) or {}).get("verdict") == "pass"
                           for r in ("install", "firstboot"))]
        if none:
            raise GateError(f"no passed install recorded for {', '.join(none)}; deploy them "
                            "from install")
    return targets, w, incomplete


def sweep_interrupted_installs(db, g, *, pxe_dir: str = pxeboot.PXE_DIR) -> list[str]:
    """Remove the hostname iPXE file of every device whose latest install row was
    interrupted (a daemon restart mid-install: its finally never ran, so the file and the
    Pxe override stay and the next power-on would reinstall). Each removal, or a removal
    that failed, is an event; returns the positions acted on."""
    lr = db.latest_runs()
    acted = []
    for a in plan_addresses(g):
        if a.role != "host" or (lr.get((a.pos, "install")) or {}).get("status") != "interrupted":
            continue
        if not pxeboot.exists(a.name, pxe_dir=pxe_dir):
            continue
        gone, err = pxeboot.remove(a.name, pxe_dir=pxe_dir)
        db.add_event("deploy", f"{a.pos}: install interrupted by a restart; hostname-{a.name}"
                               ".ipxe " + ("removed (a power-on no longer reinstalls)" if gone
                                           else f"NOT removed ({err}): a power-on would "
                                                "reinstall"))
        acted.append(a.pos)
    return acted


# ---------------------------------------------------------------- recording
def _save(ctx, pos, rung, rid, res: RungResult) -> RungResult:
    """res with its text artifacts written under artifacts/<pos>/d<rid>/<rung>/ (the
    row keeps the paths; an unsafe name is dropped, never a path)."""
    arts = res.artifacts or {}
    if not arts:
        return res
    detail = dict(res.detail)
    keep = {n.removesuffix(".txt"): t for n, t in arts.items()
            if _safe(n.removesuffix(".txt")) and isinstance(t, str)}
    if len(keep) < len(arts):
        detail["dropped_artifacts"] = sorted(n for n in arts if n.removesuffix(".txt") not in keep)
    rel = dict(zip(keep, agent.save_artifacts(ctx.dir, pos, f"d{rid}", rung, keep)))
    return RungResult(res.verdict, detail, rel)


def _finish(ctx, rid, pos, rung, res: RungResult) -> RungResult:
    try:
        r = _save(ctx, pos, rung, rid, res)
    except Exception as e:                  # noqa: BLE001 -- a row never stays 'running'
        r = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
    ctx.db.finish_run(rid, r)
    return r


def _rung(ctx, pos, rung, fn) -> RungResult:
    """A 'running' row for the UI while fn runs, then its result (artifacts saved)."""
    rid = ctx.db.start_run(pos, STAGE, rung)
    try:
        res = fn()
    except Exception as e:                  # noqa: BLE001
        res = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
    return _finish(ctx, rid, pos, rung, res)


def _record(ctx, pos, rung, res: RungResult) -> RungResult:
    return _finish(ctx, ctx.db.start_run(pos, STAGE, rung), pos, rung, res)


# ---------------------------------------------------------------- phase plumbing
@dataclass
class _Dev:
    """One target device's walk through the phases."""
    d: object
    ips: dict
    row: dict
    hostname: str | None
    failed: str | None = None       # the phase it failed in (later phases skip it)
    unmeasured: bool = False        # ... because its rung there was not measured
    sol: object = None              # the running SOL capture
    sol_rid: int | None = None      # the install-console row, 'running' until scanned
    sol_done: bool = False

    @property
    def pos(self) -> str:
        return self.d.pos

    @property
    def ha(self) -> bool:
        return "ha-config" in deploy_rungs(self.d)


def _why_out(x) -> str:
    if x.failed == "cancelled":
        return "cancelled"
    return f"{'not measured' if x.unmeasured else 'failed'} in phase {x.failed}"


def _out(x, phase, r: RungResult):
    if r.verdict == "pass" or x.failed:
        return
    x.failed = phase
    x.unmeasured = r.verdict == UNMEASURED


def _cancel_out(ctx, xs) -> bool:
    if not cancelled(ctx):
        return False
    for x in xs:
        x.failed = x.failed or "cancelled"
    return True


def _applies(phase, x) -> bool:
    return PHASE_RUNG[phase] in deploy_rungs(x.d)


def _skip_phase(ctx, x, phase):
    if _applies(phase, x):
        _record(ctx, x.pos, PHASE_RUNG[phase], RungResult("skip", {"reason": _why_out(x)}))


def _wave(ctx, phase, xs, fn, workers, prog):
    """fn(x) for every x at once (at most `workers` at a time); returns when all are
    done. A device whose step raised is out of the phase; the rack carries on."""
    if not xs:
        return

    def one(x):
        try:
            fn(x)
        except Exception as e:              # noqa: BLE001 -- recording itself failed
            x.failed = x.failed or phase
            ctx.db.add_event("error", f"deploy {x.pos} {phase}: {type(e).__name__}: {e}")
        finally:
            prog.done(x.pos)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(xs))),
                            thread_name_prefix=f"deploy-{ctx.g.url_prefix}-{phase}") as ex:
        list(ex.map(one, xs))


def _phase_end(ctx, phase, xs):
    ran = [x for x in xs if (x.failed is None or x.failed == phase) and _applies(phase, x)]
    bad = [x.pos for x in ran if x.failed == phase and not x.unmeasured]
    unm = [x.pos for x in ran if x.failed == phase and x.unmeasured]
    ctx.db.add_event("deploy", f"phase {phase}: {len(ran) - len(bad) - len(unm)} passed"
                               + (f", failed: {', '.join(bad)}" if bad else "")
                               + (f", not measured: {', '.join(unm)}" if unm else ""))


# ---------------------------------------------------------------- SOL (install-console)
def _sol_rel(pos, rid) -> str:
    return f"artifacts/{pos}/d{rid}/{CONSOLE}/sol.txt"


def _sol_path(ctx, x):
    return ctx.dir / _sol_rel(x.pos, x.sol_rid) if x.sol_rid is not None else None


def _sol_start(ctx, R, cr, x):
    """Open the install-console row ('running') and start the SOL capture into the
    order dir; a capture that cannot start never stops the install (the scan then
    finds nothing to scan: not measured)."""
    if x.sol_rid is not None or not _safe(x.pos):
        return
    x.sol_rid = ctx.db.start_run(x.pos, STAGE, CONSOLE)
    try:
        x.sol = R.sol_start(x.ips["bmc"], cr.bmc, _sol_path(ctx, x), runner=ctx.runner)
    except Exception as e:                  # noqa: BLE001
        x.sol = RungResult("fail", {"error": f"sol start: {type(e).__name__}: {e}"})


def _sol_stop(ctx, R, x):
    """Stop the capture (sol deactivate, always) and finish install-console with the
    solscan verdict over the whole capture; the raw SOL is its `sol` artifact."""
    if x.sol_rid is None:
        return
    rid, cap = x.sol_rid, x.sol
    path = _sol_path(ctx, x)
    x.sol_rid, x.sol, x.sol_done = None, None, True
    try:
        capres = cap if isinstance(cap, RungResult) else R.sol_stop(cap)
    except Exception as e:                  # noqa: BLE001 -- the row never stays running
        capres = RungResult("fail", {"error": f"sol stop: {type(e).__name__}: {e}"})
    try:
        scan = R.sol_scan(path)
    except Exception as e:                  # noqa: BLE001
        scan = RungResult("fail", {"error": f"solscan: {type(e).__name__}: {e}",
                                   "rows": [], "notes": []})
    try:
        n = path.stat().st_size
    except OSError:
        n = 0
    cd = capres.detail
    det = dict(scan.detail)
    det["capture"] = {k: cd.get(k) for k in ("bytes", "cipher", "user", "seconds", "drops",
                                             "error", "note") if cd.get(k) is not None}
    if capres.verdict != "pass" and scan.verdict == UNMEASURED and cd.get("error"):
        det["headline"] = det["reason"] = det["error"] = f"{cd['error']}; nothing to scan"
    det["rows"] = [["capture", f"sol.txt, {n} bytes" if n else "none"],
                   ["cipher", str(cd.get("cipher") or "")],
                   ["session drops", str(cd.get("drops") or 0)]] + list(det.get("rows") or [])
    det["notes"] = ["SOL from just before the install power action until the first boot of "
                    "the installed OS was proven (or the device left Deploy)"] + \
        list(det.get("notes") or [])
    ctx.db.finish_run(rid, RungResult(scan.verdict, det, {"sol": _sol_rel(x.pos, rid)} if n else {}))


def _sol_end(ctx, R, xs):
    """Every device's install-console is recorded once: scanned if a capture ran, else
    skipped with the reason it is out."""
    for x in xs:
        try:
            if x.sol_rid is not None:
                _sol_stop(ctx, R, x)
            elif not x.sol_done:
                x.sol_done = True
                _record(ctx, x.pos, CONSOLE, RungResult("skip", {
                    "reason": _why_out(x) if x.failed else "no sol capture started"}))
        except Exception as e:              # noqa: BLE001
            ctx.db.add_event("error", f"deploy {x.pos} {CONSOLE}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- install / firstboot
def _install(ctx, R, cr, x):
    if _cancel_out(ctx, [x]):               # not started: never send a power action
        _record(ctx, x.pos, "install", RungResult("skip", {"reason": "cancelled"}))
        return
    bmc_ip, host_ip = x.ips.get("bmc"), x.ips.get("host")
    if not bmc_ip or not host_ip or not x.hostname:
        r = _record(ctx, x.pos, "install", RungResult("fail", {
            "error": "no BMC/host address or hostname in the address plan"}))
    else:
        _sol_start(ctx, R, cr, x)           # capture the whole install
        r = _rung(ctx, x.pos, "install", lambda: R.install(
            bmc_ip, host_ip, x.hostname, cr, sol_path=_sol_path(ctx, x),
            deadline_s=DEADLINES["install"], runner=ctx.runner))
    _out(x, "install", r)


def _firstboot(ctx, R, cr, x):
    _sol_start(ctx, R, cr, x)               # --from firstboot: the capture starts here
    r = _rung(ctx, x.pos, "firstboot", lambda: R.firstboot(
        x.ips["bmc"], x.ips["host"], x.hostname, cr, expect_mac=x.row.get("host_mac"),
        sol_path=_sol_path(ctx, x), deadline_s=DEADLINES["firstboot"], runner=ctx.runner))
    _out(x, "firstboot", r)


# ---------------------------------------------------------------- user
def _user(ctx, R, cr, prog, xs):
    if not xs:
        return
    rids = {x.pos: ctx.db.start_run(x.pos, STAGE, "flax-user") for x in xs}
    try:
        if not cr.host:
            why = "add credentials-host.json (flax row)"
            out = {p: RungResult("fail", {"error": why, "headline": why}) for p in rids}
        else:
            user, pw = cr.host
            out = R.flax_user({x.pos: x.ips["host"] for x in xs}, user, pw, ctx.dir,
                              timeout=DEADLINES["user"], runner=ctx.runner)
    except Exception as e:                  # noqa: BLE001
        out = {p: RungResult("fail", {"error": f"{type(e).__name__}: {e}"}) for p in rids}
    for x in xs:
        r = _finish(ctx, rids[x.pos], x.pos, "flax-user",
                    out.get(x.pos) or RungResult("fail", {"error": "no flax_user result"}))
        _out(x, "user", r)
        prog.done(x.pos)


# ---------------------------------------------------------------- HA
def ha_inputs(ctx, lr, d, host_ip) -> tuple[dict | None, str | None, str | None]:
    """(sides, A-side cidr, None) for bond.configure / hatest from Qualify's evidence:
    its ha-path links (the wedge LLDP saw each port on) and the live image's
    ifname -> MAC (the inventory's ip-d-link); else (None, None, why not)."""
    ha = lr.get((d.pos, "ha-path")) or {}
    if ha.get("verdict") != "pass":
        return None, None, (f"Qualify ha-path {ha.get('verdict') or 'not run'}: no proven "
                            "wedge side per port")
    text = _artifact(ctx, lr, d.pos, "inventory", "ip-d-link")
    ifmac = parse_ip_link(text) if text else {}
    if not ifmac:
        return None, None, "no live-image MACs (Qualify inventory ip-d-link artifact)"
    side, err = bond.sides((ha.get("detail") or {}).get("links") or [], ifmac)
    if err:
        return None, None, err
    try:
        cidr = fabric.fabric_addr(ctx.g, d, "A", int(host_ip.rsplit(".", 1)[1]))
    except (ValueError, AttributeError) as e:
        return None, None, str(e)
    return side, cidr, None


def _ha_config(ctx, R, cr, lr, x):
    host_ip = x.ips["host"]
    side, cidr, why = ha_inputs(ctx, lr, x.d, host_ip)
    if why:
        r = _record(ctx, x.pos, "ha-config", RungResult("fail", {"error": why, "headline": why}))
    else:
        user, pw = cr.host
        r = _rung(ctx, x.pos, "ha-config", lambda: R.bond_config(
            host_ip, user, pw, side, cidr, runner=ctx.runner))
    _out(x, "ha-config", r)


def _ha_node(pos, host_ip, side, cidr) -> dict:
    return {"pos": pos, "host_ip": host_ip, "bond_ip": cidr.split("/")[0],
            "macs": {s: side[s]["mac"] for s in ("A", "B")}}


def _ha_test(ctx, R, cr, prog, ips, xs, all_xs):
    """One whole-wedge test over xs (+ the iperf server when outside this run)."""
    g = ctx.g
    srv = g.iperf_server
    lr = ctx.db.latest_runs()
    nodes, why, extra = [], None, None
    for x in xs:
        side, cidr, err = ha_inputs(ctx, lr, x.d, x.ips["host"])
        if err:                             # ha-config passed on these inputs: cannot happen
            why = f"{x.pos}: {err}"
            break
        nodes.append(_ha_node(x.pos, x.ips["host"], side, cidr))
    sx = next((x for x in all_xs if x.pos == srv), None)
    srv_dev = next((d for d in g.devices if d.pos == srv), None)
    if why:
        pass
    elif not srv:
        why = "no iperf_server in geometry.json"
    elif srv_dev is None or "ha-config" not in deploy_rungs(srv_dev):
        why = f"server {srv} not deployed: not a host with PCIe fabric ports in this rack"
    elif sx is not None:
        if sx not in xs:
            why = f"server {srv} not deployed: {_why_out(sx)}"
    elif (lr.get((srv, "ha-config")) or {}).get("verdict") != "pass":
        why = f"server {srv} not deployed: its ha-config has not passed"
    else:
        hip = ips.get(srv, {}).get("host")
        side, cidr, err = ha_inputs(ctx, lr, srv_dev, hip) if hip else (None, None, "no host IP")
        if err:
            why = f"server {srv} not deployed: {err}"
        else:
            extra = srv
            nodes.append(_ha_node(srv, hip, side, cidr))
    parts = [x.pos for x in xs] + ([extra] if extra else [])
    rids = {p: ctx.db.start_run(p, STAGE, "ha-test") for p in parts}
    try:
        if why:
            out = {p: RungResult("fail", {"error": why, "headline": why}) for p in parts}
        else:
            user, pw = cr.host
            out = R.ha_test(nodes, srv, user=user, pw=pw, runner=ctx.runner)
    except Exception as e:                  # noqa: BLE001
        out = {p: RungResult("fail", {"error": f"{type(e).__name__}: {e}"}) for p in parts}
    res = {}
    for p in parts:
        res[p] = _finish(ctx, rids[p], p, "ha-test",
                         out.get(p) or RungResult("fail", {"error": "no ha-test result"}))
        prog.done(p)
    for x in xs:
        _out(x, "ha-test", res[x.pos])
    _record_ha_mode(ctx)


def ha_mode_of(lr, g) -> dict | None:
    """The delivered HA mode from the latest runs: the devices whose latest ha-test
    passed and the fabric-cabled hosts still missing one (None while none passed)."""
    ha_devs = [d.pos for d in g.devices if "ha-test" in deploy_rungs(d)]
    ok = [p for p in ha_devs if (lr.get((p, "ha-test")) or {}).get("verdict") == "pass"]
    if not ok:
        return None
    return {"mode": bond.HA_MODE, "devices": ok, "missing": [p for p in ha_devs if p not in ok],
            "notes": hatest.NOTES[:1]}


def _record_ha_mode(ctx):
    ha = ha_mode_of(ctx.db.latest_runs(), ctx.g)
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ctx.db.set_ha_mode(ha and {**ha, "at": at})


# ---------------------------------------------------------------- stage
def run_deploy(ctx, *, positions: list[str] | None = None, from_phase: str = "install",
               reinstall: str | None = None, waive: dict | None = None,
               who: str = "?") -> None:
    if ctx.db.closed_at():
        raise RuntimeError("order is closed")
    g = ctx.g
    lr = ctx.db.latest_runs()
    targets, waivers, incomplete = plan(lr, g, positions=positions, from_phase=from_phase,
                                        reinstall=reinstall, waive=waive)
    who = clean_who(who)
    cr = creds(ctx)
    R = ctx.rungs or real_rungs()
    ips: dict[str, dict] = {}
    names: dict[str, str] = {}
    for a in plan_addresses(g):
        ips.setdefault(a.pos, {})[a.role] = a.ip
        if a.role == "host":                # = the Kea reservation's hostname
            names[a.pos] = a.name
    ctx.db.add_event("deploy", f"deploy started from {from_phase}: "
                               + ", ".join(d.pos for d in targets)
                               + (" (reinstall confirmed)" if reinstall == g.order else ""))
    if incomplete:
        ctx.db.add_event("deploy", "rack incomplete: "
                         + ", ".join(f"{p} ({y})" for p, y in incomplete.items()))
    for pos, w in waivers.items():
        ctx.db.add_waiver(pos, STAGE, w["rungs"], w["reason"], who)
        state = ", ".join(f"{r} {(lr.get((pos, r)) or {}).get('verdict') or 'not run'}"
                          for r in w["rungs"])
        ctx.db.add_event("deploy", f"gate waiver {pos}: {state} -- {w['reason']} (by {who})")
    rows = {x["pos"]: x for x in ctx.db.devices()}
    xs = [_Dev(d, ips.get(d.pos, {}), rows.get(d.pos) or {"pos": d.pos}, names.get(d.pos))
          for d in targets]
    prog = _Progress(ctx, PHASES, ctx.db.set_deploy_progress)
    try:
        for phase in PHASES[PHASES.index(from_phase):]:
            if phase != "install":          # install checks Cancel per device
                _cancel_out(ctx, xs)
            for x in xs:
                if x.failed:
                    _skip_phase(ctx, x, phase)
            run = [x for x in xs if not x.failed and _applies(phase, x)]
            prog.start(phase, [x.pos for x in run])
            if phase == "install":
                _wave(ctx, phase, run, lambda x: _install(ctx, R, cr, x), MAX_PARALLEL, prog)
            elif phase == "firstboot":
                _wave(ctx, phase, run, lambda x: _firstboot(ctx, R, cr, x), MAX_PARALLEL, prog)
            elif phase == "user":
                _user(ctx, R, cr, prog, run)
            elif phase == "ha-config":
                lr = ctx.db.latest_runs()
                _wave(ctx, phase, run, lambda x: _ha_config(ctx, R, cr, lr, x), len(run), prog)
            elif run:
                _ha_test(ctx, R, cr, prog, ips, run, xs)
            _phase_end(ctx, phase, xs)
            # out of Deploy or past the first boot: its console is complete
            _sol_end(ctx, R, [x for x in xs if x.failed or phase == "firstboot"])
    finally:
        _sol_end(ctx, R, xs)                # always: no SOL session outlives the stage
        prog.clear()
    lr = ctx.db.latest_runs()
    why = deploy_why(lr, g)
    bad = [d.pos for d in targets if d.pos in why]
    done = "deploy cancelled" if cancelled(ctx) else "deploy done"
    ctx.db.add_event("deploy", f"{done}; deployed: "
                     + (", ".join(d.pos for d in targets if d.pos not in why) or "none")
                     + (f"; not deployed: {', '.join(f'{p} ({why[p]})' for p in bad)}"
                        if bad else ""))


_SAFE_WHO = re.compile(r"[^\w .@:+-]")


def clean_who(who) -> str:
    """A short printable 'who' for a waiver row (an operator name or the client)."""
    s = _SAFE_WHO.sub("", str(who or "")).strip()[:64]
    return s or "?"
