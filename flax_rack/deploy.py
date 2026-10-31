"""Deploy stage for a rack order (spec sections 7.2a, 7.3, 7.3b), run in lockstep phases.

Five rack-wide phases, each started on every still-eligible device at once, with a
barrier (wait for all) before the next:

  install    SOL capture starts; install.install: hostname iPXE file, Redfish Pxe +   3600 s
             power cycle, the wiwynn-tp autoinstall powers the node off
  firstboot  install.firstboot: Redfish Hdd, power on, ssh as flax, installed-OS     1200 s
             proof; then the SOL capture stops and is scanned (install-console)
  user       ansible.flax_user: flax_user.yml once over every node still in         1800 s
  ha-config  bond.configure: bond0 active-backup over the two PCIe ports (by MAC),    300 s
             primary = the wedge-A port (live wedge LLDP, else Qualify's ha-path),
             A-side fabric address
  ha-test    hatest.run once for the rack: whole-wedge cut A, then B, iperf mesh     1200 s
             over bond0 against geometry.iperf_server
  iperf      deploy-iperf: iperf.mesh once for the rack against geometry.iperf_server  900 s
             over mgmt (the installed OS's interface holding the host IP) and bond0
             (ha-config's address), both directions, 0.8 x the client's link speed
             (ethtool; bond0's is its active slave's); iperf3 apt-installed where missing
  stress     deploy-stress: stress.run on every node at once (stress-ng --cpu 0 --vm,    1200 s
             600 s), SEL/SDR before + after, the kernel log's delta scanned for
             MCE/EDAC/thermal; fail on a new non-benign SEL entry (transients benign),
             a dmesg hit, no ssh after, or stress-ng rc != 0

The install/firstboot/user/stress deadlines are the rungs' own (deadline_s / timeout);
ha-config, ha-test and iperf are bounded by their rungs' ssh timeouts and settle windows
(iperf: one client at a time, both directions per network).

deploy-iperf's server is geometry.iperf_server: one of this run's devices (it serves only
if still in Deploy), else one whose latest Deploy rungs up to ha-test passed (it joins to
serve; its deploy-iperf row is written only when a client measured against it, so a
scoped Deploy never overwrites its own result).

Gate: a device enters Deploy only when its latest Qualify passed (qualify.device_rungs
all passed and Post's verdict pass). Rack-wide, every host-bearing device must pass; a
`positions` run needs each listed device to pass and records "rack incomplete: ..." as an
event for the others. A recorded WAIVER (operator, per device: the named Qualify rungs,
a reason, who, when -- stored in order.db) lets a device through when every rung it has
not passed is named in it; any other failed/unmeasured rung still blocks. A waiver is
carried by the request that uses it (never inherited from an earlier Deploy).

Reinstall guard: a target with an installed OS (an install or first boot passed in ANY
pass -- Start fresh resets verdicts, not the disk -- or this pass's Discover found it up
on its installed OS) is only reinstalled (its disks wiped) with reinstall=<order id>;
else the run is refused and `from_phase="firstboot"` (skip install: SOL, Hdd boot,
installed-OS proof, then the rest) is the way on. A firstboot run needs that same
installed-OS evidence for each target.

From iperf / stress (LATE_FROM) re-runs just those phases on deployed nodes: no power
action, no install, no Qualify gate (the node already went through it). Each target
needs its earlier Deploy phases passed in this pass (deploy_why before deploy-iperf:
install-or-kept / firstboot / flax-user / ha-config / ha-test), or an installed OS (any
pass) with bond0 up (this pass's ha-config passed; a node without fabric links: the
installed OS alone); anything else is refused naming what is missing.

A device failing a phase (fail or unmeasured on its rung) gets `skip` ("failed in phase
<p>" / "not measured in phase <p>" / "cancelled") on the later phases' rungs; the rest
carry on. install-console never gates the later phases (Qualify's console rule) but a
fail there still fails the device in deploy_passed. The rack's Cancel is checked before
each device's install and at every barrier (an install already powering the node runs
to its end). The current phase is kept in order.db (deploy_progress) for the UI.

ha-config's wedge sides come from LIVE LLDP (the rack's LinkPoller snapshot, else one
read of each wedge): the wedge whose LLDP table names this node (<prefix><pos>) with the
port's MAC is that port's side. A port the installer left admin-down sends no LLDP, so
when live LLDP does not show both sides the fabric ports (MACs from live LLDP + Qualify's
ha-path/inventory) are set up on the node (bond.bring_up, by permanent MAC, never the
mgmt NIC) and live LLDP is re-read for up to LLDP_WAIT_POLLS poll intervals. Only when
live LLDP shows nothing for the node does Qualify's ha-path decide (it must have
passed); one side live and the other not, or neither source, fails naming what was
seen. ha-test takes each node's sides + bond address from its passed ha-config row, so
an `ha-path` waiver no longer strands a deployed node.

ha-test needs the iperf server: when it is not one of this run's devices, its latest
ha-config must have passed (it joins the test: its wedge ports are cut too), else every
participant fails "server <pos> not deployed". After a test the order records the HA
mode delivered (order.db ha_mode: mode, devices whose latest ha-test passed, missing).

flax_rungs functions never touch order.db; this module records them. ctx.rungs (a
namespace like real_rungs()) replaces them in tests and in RACKPOST_FAKE.
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from flax_rungs import (agent, ansible, bond, fabric, hatest, hostssh, install, iperf, pxeboot,
                        sol, solscan, sonic, stress)
from flax_rungs.remote import unreachable
from flax_rungs.result import UNMEASURED, RungResult, not_passed

from . import linkmon
from .addressing import plan_addresses
from .qualify import (AGENT_STAGES, _Progress, _artifact, _measured, _safe, _stage_ok,
                      device_rungs, device_verdict, iface_of, parse_ip_link)
from .stages import cancelled, creds

STAGE = "deploy"
PHASES = ("install", "firstboot", "user", "ha-config", "ha-test", "iperf", "stress")
FROM_PHASES = ("install", "firstboot", "iperf", "stress")
# a re-run of just these on deployed nodes (no power action, no install or gate)
LATE_FROM = ("iperf", "stress")
DEADLINES = {"install": 3600, "firstboot": 1200, "user": 1800, "ha-config": 300,
             "ha-test": 1200, "iperf": 900, "stress": 1200}
PHASE_RUNG = {"install": "install", "firstboot": "firstboot", "user": "flax-user",
              "ha-config": "ha-config", "ha-test": "ha-test", "iperf": "deploy-iperf",
              "stress": "deploy-stress"}
CONSOLE = "install-console"
NON_GATING = (CONSOLE,)
MAX_PARALLEL = 8            # concurrent installs / first boots (BMC power actions)
LLDP_WAIT_POLLS = 2         # ha-config: link-monitor intervals to wait for a raised port's LLDP
_REASON_MAX = 500


class GateError(ValueError):
    """Deploy refused before anything ran (gate, reinstall guard, a bad waiver)."""


def real_rungs():
    return SimpleNamespace(
        install=install.install, firstboot=install.firstboot, sol_start=sol.start,
        sol_stop=sol.stop, sol_scan=solscan.scan_file, flax_user=ansible.flax_user,
        bond_config=bond.configure, fabric_up=bond.bring_up, ha_test=hatest.run,
        read_live=sonic.read_live, mesh=iperf.mesh, host_run=hostssh.run, stress=stress.run,
        clock=time.monotonic, sleep=time.sleep)


def deploy_rungs(d) -> list[str]:
    """The Deploy rungs of a device ([] = no host); ha-* only with PCIe fabric links."""
    if not d.endpoints.get("hosts"):
        return []
    out = ["install", "firstboot", CONSOLE, "flax-user"]
    if any(n.startswith("pcie") for n in d.links):
        out += ["ha-config", "ha-test"]
    return out + ["deploy-iperf", "deploy-stress"]


def deploy_why(lr, g, before: str | None = None) -> dict[str, str]:
    """pos -> why it is not deployed (absent = deployed): its first Deploy rung (step
    order) whose latest run is not pass. An install not run in this pass, or skipped (a
    Deploy from firstboot kept the installed disk), is no blocker when this pass's
    firstboot passed (the first boot proved the installed OS). before: only the rungs
    ahead of that one count."""
    out = {}
    for d in g.devices:
        rungs = deploy_rungs(d)
        for r in (rungs[:rungs.index(before)] if before in rungs else rungs):
            v = (lr.get((d.pos, r)) or {}).get("verdict")
            if r == "install" and v in (None, "skip") \
                    and (lr.get((d.pos, "firstboot")) or {}).get("verdict") == "pass":
                continue
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


def installed_positions(db, lr=None) -> set[str]:
    """Positions whose disk holds an installed OS: an install (or first boot) passed in
    ANY pass, or this pass's Discover found the node up on it. A fact about the disk,
    not a verdict: Start fresh does not forget it (the reinstall guard rests on it)."""
    lr = db.latest_runs() if lr is None else lr
    return db.ever_passed(("install", "firstboot")) | _installed_now(lr)


def _installed_now(lr) -> set[str]:
    """This pass's evidence: a passed install / first boot, or Discover's installed-OS
    probe (power-on / host-ssh passed with installed_os)."""
    return {pos for (pos, rung), r in lr.items() if r.get("verdict") == "pass" and (
        rung in ("install", "firstboot")
        or (rung in ("power-on", "host-ssh") and (r.get("detail") or {}).get("installed_os")))}


# the wiwynn-tp autoinstall's storage config writes this disk (path: /dev/nvme0n1, gpt,
# wipe: superblock); its first boot proves "/ on nvme0n1"
INSTALL_DISK = "nvme0n1"
_BLKID_DISK = re.compile(rf"^/dev/{INSTALL_DISK}(p\d+)?:", re.M)
NOT_BLANK = "disk not blank / unknown — reinstall needs the typed order id"


def disk_blank(odir, lr, pos) -> tuple[bool | None, str]:
    """Is the install disk (INSTALL_DISK) of `pos` blank, by the live image's inventory
    in `lr` (its lsblk + blkid captures)? (True, why): lsblk lists it as a disk with no
    child row (no partition, LVM, md, dm) and blkid names no signature on it or its
    partitions. (False, why): something is on it. (None, why): not known (no inventory
    row, a capture missing or empty, the disk not listed)."""
    arts = (lr.get((pos, "inventory")) or {}).get("artifacts") or {}

    def text(name):
        try:
            return (Path(odir) / arts[name]).read_text() if arts.get(name) else None
        except OSError:
            return None
    lsblk, blkid = text("lsblk"), text("blkid")
    if not lsblk or not lsblk.lstrip().startswith("NAME") or not (blkid or "").strip():
        return None, f"no inventory lsblk/blkid capture for {INSTALL_DISK}"
    lines = lsblk.splitlines()[1:]
    at = next((i for i, ln in enumerate(lines) if ln.split()[:1] == [INSTALL_DISK]), None)
    if at is None or "disk" not in lines[at].split()[5:6]:
        return None, f"{INSTALL_DISK} not listed as a disk by lsblk"
    nxt = lines[at + 1] if at + 1 < len(lines) else ""
    if nxt and not nxt[:1].isalnum():
        return False, f"{INSTALL_DISK} has {nxt.split()[0].lstrip('├└│─ ')} (lsblk)"
    if m := _BLKID_DISK.search(blkid):
        return False, f"blkid: {m.group(0).rstrip(':')} has a signature"
    return True, f"{INSTALL_DISK} blank (lsblk: no partition; blkid: no signature)"


def blank_positions(odir, lr, positions) -> set[str]:
    return {p for p in positions if disk_blank(odir, lr, p)[0] is True}


def plan(lr, g, *, positions=None, from_phase="install", reinstall=None, waive=None,
         installed=None, firstboot=(), blank=None):
    """(target devices, waivers, {pos: why} of the other host devices not passed). Raises
    GateError when the run may not start: unknown position, the gate (with the waivers),
    or an installed disk (`installed`: installed_positions(db), across passes; this pass's
    runs when None) without reinstall=<order id>. `firstboot`: targets of an install run
    that start at firstboot instead (installed disks a Run does not reinstall); each must
    be installed. `blank` (blank_positions; None = not checked): without reinstall, an
    install target with no install record must have its install disk proven blank."""
    if installed is None:
        installed = _installed_now(lr)
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
    if from_phase in LATE_FROM:
        pre = deploy_why(lr, g, before="deploy-iperf")

        def bond_up(d):
            return "ha-config" not in deploy_rungs(d) \
                or (lr.get((d.pos, "ha-config")) or {}).get("verdict") == "pass"
        bad = {d.pos: pre[d.pos] for d in targets
               if d.pos in pre and not (d.pos in installed and bond_up(d))}
        if bad:
            raise GateError(f"deploy from {from_phase} needs the earlier Deploy phases passed "
                            "(or an installed OS with bond0 up): "
                            + "; ".join(f"{p} {y}" for p, y in bad.items()))
        return targets, {}, {}          # a waiver is for the gate, which this never passes
    blocked = {d.pos: why for d in targets
               if (why := gate_why(lr, d, w.get(d.pos, {}).get("rungs", ())))}
    if blocked:
        scope = "rack-wide Deploy needs every host-bearing device passed; " if not positions else ""
        raise GateError("deploy gate: " + scope + "; ".join(f"{p} {y}" for p, y in blocked.items()))
    tset = {d.pos for d in targets}
    incomplete = {d.pos: why for d in hosts if d.pos not in tset and (why := gate_why(lr, d))}
    fb = set(firstboot)
    if fb - tset:
        raise GateError(f"firstboot: not a target: {', '.join(sorted(fb - tset))}")
    if from_phase == "install":
        done = [d.pos for d in targets if d.pos in installed and d.pos not in fb]
        if done and reinstall != g.order:
            raise GateError(
                f"already installed: {', '.join(done)}; a reinstall wipes their disks -- "
                f"confirm with reinstall {g.order} (CLI --reinstall {g.order}), or deploy "
                f"from firstboot (CLI --from firstboot)")
        unknown = [d.pos for d in targets
                   if blank is not None and reinstall != g.order and d.pos not in installed
                   and d.pos not in fb and d.pos not in blank]
        if unknown:
            raise GateError(f"{', '.join(unknown)}: {NOT_BLANK} (CLI --reinstall {g.order})")
    else:
        fb = tset
    # a first boot of a never-installed disk boots whatever is on it, then fails slowly
    none = [d.pos for d in targets if d.pos in fb and d.pos not in installed]
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


def _sol_start(ctx, R, x):
    """Open the install-console row ('running') and start the SOL capture into the
    order dir; a capture that cannot start never stops the install (the scan then
    finds nothing to scan: not measured)."""
    if x.sol_rid is not None or not _safe(x.pos):
        return
    x.sol_rid = ctx.db.start_run(x.pos, STAGE, CONSOLE)
    try:
        x.sol = R.sol_start(x.ips["bmc"], _sol_path(ctx, x), reason="rackpost-deploy")
    except Exception as e:                  # noqa: BLE001
        x.sol = RungResult("fail", {"error": f"sol start: {type(e).__name__}: {e}"})


def _sol_stop(ctx, R, x):
    """Stop the capture (the rest of the relay's console copied in) and finish
    install-console with the solscan verdict over the whole capture; the raw SOL is its
    `sol` artifact."""
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
    det["capture"] = {k: cd.get(k) for k in ("bytes", "source", "seconds", "rotations",
                                             "error", "note") if cd.get(k) is not None}
    if capres.verdict != "pass" and scan.verdict == UNMEASURED and cd.get("error"):
        det["headline"] = det["reason"] = det["error"] = f"{cd['error']}; nothing to scan"
    det["rows"] = [["capture", f"sol.txt, {n} bytes" if n else "none"],
                   ["source", str(cd.get("source") or "")],
                   ["file rotations", str(cd.get("rotations") or 0)]] + list(det.get("rows") or [])
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
        _sol_start(ctx, R, x)           # capture the whole install
        r = _rung(ctx, x.pos, "install", lambda: R.install(
            bmc_ip, host_ip, x.hostname, cr, sol_path=_sol_path(ctx, x),
            deadline_s=DEADLINES["install"], runner=ctx.runner))
    _out(x, "install", r)


def _firstboot(ctx, R, cr, x):
    _sol_start(ctx, R, x)               # --from firstboot: the capture starts here
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
def _cidr(ctx, d, host_ip) -> tuple[str | None, str | None]:
    try:
        return fabric.fabric_addr(ctx.g, d, "A", int(host_ip.rsplit(".", 1)[1])), None
    except (ValueError, AttributeError) as e:
        return None, str(e)


def _qualify_ifmac(ctx, lr, pos) -> dict[str, str]:
    text = _artifact(ctx, lr, pos, "inventory", "ip-d-link")
    return parse_ip_link(text) if text else {}


def ha_inputs(ctx, lr, d, host_ip) -> tuple[dict | None, str | None, str | None]:
    """(sides, A-side cidr, None) from Qualify's evidence: its ha-path links (the wedge
    LLDP saw each port on) and the live image's ifname -> MAC (the inventory's
    ip-d-link); else (None, None, why not). ha-config's fallback when live LLDP shows
    nothing for the node."""
    ha = lr.get((d.pos, "ha-path")) or {}
    if ha.get("verdict") != "pass":
        return None, None, (f"Qualify ha-path {ha.get('verdict') or 'not run'}: no proven "
                            "wedge side per port")
    ifmac = _qualify_ifmac(ctx, lr, d.pos)
    if not ifmac:
        return None, None, "no live-image MACs (Qualify inventory ip-d-link artifact)"
    side, err = bond.sides((ha.get("detail") or {}).get("links") or [], ifmac)
    if err:
        return None, None, err
    cidr, err = _cidr(ctx, d, host_ip)
    return (side, cidr, None) if cidr else (None, None, err)


def _mac12(m) -> str:
    h = re.sub(r"[^0-9a-f]", "", (m or "").lower())
    return h if len(h) == 12 else ""


def live_sides(g, snaps: dict, pos: str, now: float | None = None) -> dict[str, dict]:
    """side -> {"mac", "link" ("<wedge> <iface>"), "ifname"} from fresh wedge snapshots:
    an LLDP row on wedge A/B whose system name is the node's hostname (<prefix><pos>, or
    <that>-...) and whose port ID is a MAC. A side seen with two MACs is left out."""
    base = linkmon.device_names(g)[pos]
    out, dup = {}, set()
    for s in g.switches:
        snap = snaps.get(s.id)
        if s.id not in ("A", "B") or not snap or snap.get("at") is None:
            continue
        if now is not None and now - snap["at"] > linkmon.STALE_S:
            continue
        for e in snap.get("lldp") or []:
            e = linkmon._d(e)
            n = (e.get("remote_device") or "").strip().lower()
            m = _mac12(e.get("remote_port"))
            if not m or not (n == base or n.startswith(base + "-")):
                continue
            mac = ":".join(m[i:i + 2] for i in range(0, 12, 2))
            if s.id in out and out[s.id]["mac"] != mac:
                dup.add(s.id)
            out[s.id] = {"mac": mac, "link": f"{s.id} {e.get('local_iface')}",
                         "ifname": e.get("remote_descr") or None}
    return {k: v for k, v in out.items() if k not in dup}


def _snapshots(ctx, R) -> tuple[dict, float | None, int]:
    """(switch id -> snapshot, now for freshness, poll interval s): the rack's
    LinkPoller's when it runs, else one read of each wedge here (CLI / tests)."""
    lm = getattr(ctx, "links", None)
    if lm is not None:
        return lm.snapshots(), lm.clock(), lm.interval_s
    out = {}
    try:
        ips, (user, pw) = linkmon.switch_ips(ctx), creds(ctx).sonic
    except Exception:                       # noqa: BLE001 -- no switch logins: no live view
        return {}, None, linkmon.INTERVAL_S
    for s in ctx.g.switches:
        if s.id in ("A", "B") and ips.get(s.id):
            try:
                r = R.read_live(ips[s.id], user, pw, runner=ctx.runner)
            except Exception:               # noqa: BLE001
                continue
            if r.verdict == "pass":
                out[s.id] = {**r.detail, "at": 0}
    return out, None, linkmon.INTERVAL_S


def _silent(ctx, snaps: dict, now, sid: str) -> str | None:
    """Why wedge `sid` gave no fresh snapshot (it did not answer the link monitor's read,
    or that read is stale), else None (it answered: its LLDP just has no port of ours)."""
    snap = snaps.get(sid)
    if snap and snap.get("at") is not None and (now is None or now - snap["at"] <= linkmon.STALE_S):
        return None
    lm = getattr(ctx, "links", None)
    err = lm.switch_live(sid).get("error") if lm is not None else None
    if snap and snap.get("at") is not None:
        return f"last read {int(now - snap['at'])} s ago" + (f": {err}" if err else "")
    return err or "not read"


def _fabric_macs(ctx, lr, pos, live) -> list[str]:
    """The node's fabric port MACs: live LLDP's, then Qualify ha-path's (any verdict:
    each seen port's live-image ifname, MAC'd by the inventory's ip-d-link)."""
    out = [v["mac"] for v in live.values()]
    links = ((lr.get((pos, "ha-path")) or {}).get("detail") or {}).get("links") or []
    ifmac = _qualify_ifmac(ctx, lr, pos) if links else {}
    for l in links:
        m = bond._mac(ifmac.get(l.get("ifname") or "", ""))
        if m and m not in out:
            out.append(m)
    return out


def ha_sides(ctx, R, cr, lr, d, host_ip) -> tuple[dict | None, dict, str | None]:
    """(sides, info, None) or (None, info, why). info: {"sides_from", "rows", "notes"}."""
    info = {"sides_from": None, "rows": [], "notes": []}
    snaps, now, interval = _snapshots(ctx, R)
    live = live_sides(ctx.g, snaps, d.pos, now)
    if len(live) < 2:
        macs = _fabric_macs(ctx, lr, d.pos, live)
        if not macs:
            info["notes"].append("no fabric port MAC known (live LLDP, Qualify ha-path): "
                                 "none raised")
        else:
            user, pw = cr.host
            up, err = R.fabric_up(host_ip, user, pw, macs, runner=ctx.runner)
            if err:
                return None, info, f"raising the fabric ports: {err}"
            info["rows"].append(["fabric ports set up", ", ".join(
                f"{n} {m}" for m, n in sorted(up.items(), key=lambda x: x[1]))])
            wait = LLDP_WAIT_POLLS * interval
            t0 = R.clock()
            while len(live) < 2 and R.clock() - t0 < wait:
                R.sleep(min(5 if getattr(ctx, "links", None) is not None else interval / 2,
                            max(1, wait - (R.clock() - t0))))
                snaps, now, _i = _snapshots(ctx, R)
                live = live_sides(ctx.g, snaps, d.pos, now)
            info["notes"].append(f"ports set up (ip link set up, by MAC); live LLDP re-read "
                                 f"for up to {int(wait)} s")
    if len(live) == 2:
        if live["A"]["mac"] == live["B"]["mac"]:
            return None, info, "live LLDP: both wedges see the same MAC"
        info["sides_from"] = "live LLDP"
        info["rows"].append(["wedge sides from", "live LLDP: " + "; ".join(
            f"{k} {v['link'].split()[1]} {v['mac']}" for k, v in sorted(live.items()))])
        return live, info, None
    name = linkmon.device_names(ctx.g)[d.pos]
    if live:
        (k, v), = live.items()
        miss = "B" if k == "A" else "A"
        down = _silent(ctx, snaps, now, miss)
        if down:
            return None, info, (f"wedge {miss} did not answer ({down}): its side of {name} is "
                                f"not proven; live LLDP sees wedge {k} ({v['link'].split()[1]} "
                                f"{v['mac']})")
        return None, info, (f"live LLDP sees only wedge {k} ({v['link'].split()[1]} {v['mac']}); "
                            f"no port of {name} on wedge {miss}")
    side, _cidr_, why = ha_inputs(ctx, lr, d, host_ip)
    if why:
        down = [f"wedge {w} did not answer ({x})" for w in ("A", "B")
                if (x := _silent(ctx, snaps, now, w))]
        return None, info, (f"no wedge side proven: "
                            + ("; ".join(down) + ", " if down else "")
                            + f"live LLDP names no port of {name} on wedge A/B, and {why}")
    info["sides_from"] = "Qualify ha-path"
    info["rows"].append(["wedge sides from", "Qualify ha-path (live LLDP showed nothing): "
                         + "; ".join(f"{k} {v['mac']}" for k, v in sorted(side.items()))])
    return side, info, None


def _ha_config(ctx, R, cr, lr, x):
    host_ip = x.ips["host"]
    cidr, why = _cidr(ctx, x.d, host_ip)
    if why:
        r = _record(ctx, x.pos, "ha-config", RungResult("fail", {"error": why, "headline": why}))
        _out(x, "ha-config", r)
        return

    def one():
        if not cr.host:
            why = "add credentials-host.json (flax row)"
            return RungResult("fail", {"error": why, "headline": why})
        side, info, why = ha_sides(ctx, R, cr, lr, x.d, host_ip)
        if why:
            return RungResult("fail", {"error": why, "headline": why, **info})
        user, pw = cr.host
        res = R.bond_config(host_ip, user, pw, side, cidr, runner=ctx.runner)
        det = dict(res.detail)
        det["sides_from"] = info["sides_from"]
        det["rows"] = info["rows"] + list(det.get("rows") or [])
        det["notes"] = list(det.get("notes") or []) + info["notes"]
        return RungResult(res.verdict, det, res.artifacts)

    _out(x, "ha-config", _rung(ctx, x.pos, "ha-config", one))


def ha_node(lr, pos, host_ip) -> tuple[dict | None, str | None]:
    """ha-test's node from its latest passed ha-config (the sides + address it bonded)."""
    hc = lr.get((pos, "ha-config")) or {}
    det = hc.get("detail") or {}
    macs, addr = det.get("macs") or {}, det.get("address")
    if hc.get("verdict") != "pass":
        return None, "its ha-config has not passed"
    if not (macs.get("A") and macs.get("B") and addr):
        return None, "its ha-config row records no bond MACs/address"
    return {"pos": pos, "host_ip": host_ip, "bond_ip": addr.split("/")[0],
            "macs": {s: macs[s] for s in ("A", "B")}}, None


def _ha_test(ctx, R, cr, prog, ips, xs, all_xs):
    """One whole-wedge test over xs (+ the iperf server when outside this run)."""
    g = ctx.g
    srv = g.iperf_server
    lr = ctx.db.latest_runs()
    nodes, why, extra = [], None, None
    for x in xs:
        n, err = ha_node(lr, x.pos, x.ips["host"])
        if err:                             # ha-config passed in this run: cannot happen
            why = f"{x.pos}: {err}"
            break
        nodes.append(n)
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
    else:
        hip = ips.get(srv, {}).get("host")
        n, err = ha_node(lr, srv, hip) if hip else (None, "no host IP")
        if err:
            why = f"server {srv} not deployed: {err}"
        else:
            extra = srv
            nodes.append(n)
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


# ---------------------------------------------------------------- iperf
# the installed OS has no iperf3 unless ha-test put it there: apt it in where missing
IPERF_PREP = ("command -v iperf3 >/dev/null || DEBIAN_FRONTEND=noninteractive "
              "apt-get -o DPkg::Lock::Timeout=120 install -y -q iperf3 >/dev/null 2>&1\n"
              "command -v iperf3 >/dev/null || echo NOIPERF\nip -o -4 addr show\n")


def _iperf_node(ctx, R, cr, lr, d, host_ip) -> tuple[dict | None, RungResult | None]:
    """(the mesh's node {"pos", "host_ip", "ifaces"}, None) or (None, why not as a row):
    mgmt = the installed OS's interface holding the host IP, bond = ha-config's bond0."""
    user, pw = cr.host
    rc, out, err = R.host_run(host_ip, user, pw, IPERF_PREP, timeout=300, runner=ctx.runner)
    if unreachable(rc):
        why = f"{host_ip} did not answer ssh (rc={rc})"
        return None, RungResult(UNMEASURED, {"error": why, "headline": why, "reason": why})
    why = None
    if "NOIPERF" in out:
        why = "iperf3 not installed (apt-get install iperf3 failed)"
    mif = iface_of(out, host_ip)
    if why is None and not mif:
        why = f"no interface holds {host_ip} (rc={rc}) {hostssh.tail(out, err, 120)}".rstrip()
    ifaces = {"mgmt": (mif, host_ip)} if mif else {}
    if why is None and "ha-config" in deploy_rungs(d):
        n, err = ha_node(lr, d.pos, host_ip)
        if err:
            why = f"bond0: {err}"
        else:
            ifaces["bond"] = ("bond0", n["bond_ip"])
    if why:
        return None, RungResult("fail", {"error": why, "headline": why})
    return {"pos": d.pos, "host_ip": host_ip, "ifaces": ifaces}, None


def _iperf_rows(res: RungResult, node: dict | None) -> RungResult:
    """res with a row per network (the client's interface, tx / rx against 0.8 x link)."""
    det = dict(res.detail)
    rows = list(det.get("rows") or [])
    for net, v in det.items():
        if net in iperf.NETS and isinstance(v, dict):
            ifn = ((node or {}).get("ifaces") or {}).get(net, ("?",))[0]
            sp = v.get("speed_bps")
            rows.append([f"{net} ({ifn})", f"tx {hatest._gbit(v.get('tx_bps'))} / rx "
                                           f"{hatest._gbit(v.get('rx_bps'))} Gbit/s, need ≥ "
                         + (f"{iperf.THRESHOLD * sp / 1e9:.1f} (0.8 x {sp / 1e9:g})"
                            if sp else "? (no link speed)")])
    if det.get("served") is not None:
        rows.append(["served", ", ".join(det["served"]) or "none"])
    det["rows"] = rows
    return RungResult(res.verdict, det, res.artifacts)


def _iperf(ctx, R, cr, prog, ips, xs, all_xs):
    """One mesh for the rack: xs against geometry.iperf_server (+ the server, when it is
    outside this run, to serve only)."""
    g = ctx.g
    srv = g.iperf_server
    lr = ctx.db.latest_runs()
    srv_dev = next((d for d in g.devices if d.pos == srv), None)
    sx = next((x for x in all_xs if x.pos == srv), None)
    why, config, guest = None, False, None
    if not srv:
        why, config = "no iperf_server in geometry.json", True
    elif srv_dev is None or not srv_dev.endpoints.get("hosts"):
        why, config = f"server {srv} not ready: not a host-bearing device of this rack", True
    elif sx is not None:
        if sx not in xs:
            why = f"server {srv} not ready: {_why_out(sx)}"
    elif not ips.get(srv, {}).get("host"):
        why, config = f"server {srv} not ready: no host IP", True
    elif srv in (pre := deploy_why(lr, g, before="deploy-iperf")):
        why = f"server {srv} not ready: not deployed ({pre[srv]})"
    else:
        guest = srv
    clients = [x for x in xs if x.pos != srv]
    if guest is not None and not clients:
        return
    parts = [x.pos for x in xs if x.pos != guest]
    rids = {p: ctx.db.start_run(p, STAGE, "deploy-iperf") for p in parts}
    nodes, out = {}, {}
    try:
        if why is not None:
            out = {p: RungResult("fail" if config else UNMEASURED,
                                 {"error": why, "headline": why, "reason": why}) for p in parts}
        else:
            for d in [x.d for x in clients] + [srv_dev]:
                n, bad = _iperf_node(ctx, R, cr, lr, d, ips[d.pos]["host"])
                if n:
                    nodes[d.pos] = n
                else:
                    out[d.pos] = bad
            if srv not in nodes:
                head = (out[srv].detail.get("error") or "not ready")
                why = f"server {srv} not ready: {head}"
                out.update({x.pos: RungResult(UNMEASURED, {"error": why, "headline": why,
                                                           "reason": why})
                            for x in clients if x.pos in nodes})
            else:
                user, pw = cr.host
                out.update(R.mesh(nodes[srv], [nodes[x.pos] for x in clients if x.pos in nodes],
                                  user=user, pw=pw, runner=ctx.runner))
    except Exception as e:                  # noqa: BLE001
        out = {p: RungResult("fail", {"error": f"{type(e).__name__}: {e}"}) for p in parts}
    res = {}
    for p, rid in rids.items():
        r = out.get(p) or RungResult("fail", {"error": "no iperf result"})
        res[p] = _finish(ctx, rid, p, "deploy-iperf", _iperf_rows(r, nodes.get(p)))
        prog.done(p)
    if guest is not None and guest in out and any(_measured(out.get(x.pos)) for x in clients):
        _record(ctx, guest, "deploy-iperf", _iperf_rows(out[guest], nodes.get(guest)))
    for x in xs:
        _out(x, "iperf", res[x.pos])


# ---------------------------------------------------------------- stress
def _stress(ctx, R, cr, prog, xs):
    if not xs:
        return
    rids = {x.pos: ctx.db.start_run(x.pos, STAGE, "deploy-stress") for x in xs}
    try:
        user, pw = cr.host
        out = R.stress([{"pos": x.pos, "host_ip": x.ips["host"], "bmc_ip": x.ips["bmc"]}
                        for x in xs], user=user, pw=pw, bmc_creds=cr.bmc,
                       deadline_s=DEADLINES["stress"], runner=ctx.runner)
    except Exception as e:                  # noqa: BLE001
        out = {p: RungResult("fail", {"error": f"{type(e).__name__}: {e}"}) for p in rids}
    spread = next((r.detail.get("launch_spread_s") for r in out.values()
                   if r.detail.get("launch_spread_s") is not None), None)
    if spread is not None:
        ctx.db.add_event("deploy", f"stress: stress-ng launched on the nodes within "
                                   f"{spread:.1f} s")
    for x in xs:
        r = _finish(ctx, rids[x.pos], x.pos, "deploy-stress",
                    out.get(x.pos) or RungResult("fail", {"error": "no stress result"}))
        _out(x, "stress", r)
        prog.done(x.pos)


# ---------------------------------------------------------------- stage
def run_deploy(ctx, *, positions: list[str] | None = None, from_phase: str = "install",
               reinstall: str | None = None, waive: dict | None = None,
               who: str = "?", firstboot=()) -> None:
    """Deploy the targets from `from_phase`; `firstboot` names install-run targets that
    skip the install (installed disks: plan() checks them)."""
    if ctx.db.closed_at():
        raise RuntimeError("order is closed")
    g = ctx.g
    lr = ctx.db.latest_runs()
    targets, waivers, incomplete = plan(lr, g, positions=positions, from_phase=from_phase,
                                        reinstall=reinstall, waive=waive,
                                        installed=installed_positions(ctx.db, lr),
                                        blank=blank_positions(ctx.dir, lr,
                                                              [d.pos for d in g.devices]),
                                        firstboot=firstboot)
    fb = set(firstboot) if from_phase == "install" else set()
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
                               + ", ".join(d.pos for d in targets if d.pos not in fb)
                               + (f"; from firstboot (installed): {', '.join(sorted(fb))}"
                                  if fb else "")
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
    if from_phase in ("install", "firstboot"):  # from a later phase: the earlier ones' rows stand
        ctx.db.mark_stage_start(STAGE, [x.pos for x in xs])
    for x in xs:        # a late re-run captures no console: the install-console row stands
        x.sol_done = from_phase in LATE_FROM
    for x in xs:        # an installed disk kept: its install row says so (deploy_why)
        if from_phase == "firstboot" or x.pos in fb:
            # na: nothing to do by design (the disk is kept), so the phase reads complete
            why = "installed disk kept: deployed from firstboot (no reinstall)"
            _record(ctx, x.pos, "install", RungResult("skip", {"reason": why, "headline": why,
                                                               "na": True}))
    try:
        for phase in PHASES[PHASES.index(from_phase):]:
            if phase != "install":          # install checks Cancel per device
                _cancel_out(ctx, xs)
            for x in xs:
                if x.failed:
                    _skip_phase(ctx, x, phase)
            run = [x for x in xs if not x.failed and _applies(phase, x)
                   and not (phase == "install" and x.pos in fb)]
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
            elif phase == "ha-test":
                if run:
                    _ha_test(ctx, R, cr, prog, ips, run, xs)
            elif phase == "iperf":
                if run:
                    _iperf(ctx, R, cr, prog, ips, run, xs)
            else:
                _stress(ctx, R, cr, prog, run)
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
