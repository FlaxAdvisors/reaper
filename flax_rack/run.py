"""Run: one typed confirm drives Discover -> Firmware -> Qualify -> Deploy -> Done.

The operator's rule (2026-10-30): the server progresses the rack. When every device
passes, the Run goes straight through Deploy and Done. When one fails, the earlier
stages carry on with the devices that pass. The Run then HOLDS before Deploy and lists
the blockers per device. One click, Continue to Deploy, runs Deploy -> Done.

Each stage is the existing stage function; the Run adds no stage logic of its own:

  discover  run_discover, rack-wide. A device or switch with a failed or not-measured
            step (or an interrupted one) is a blocker and is left out of the rest.
  firmware  run_firmware on the devices and switches Discover passed. Detect only and
            report only (controller ruling 2026-10-30): an off-target / failed /
            unmeasured read never holds the Run and never keeps a device out of a later
            stage. Its findings go to the Run's event (the process report) and the run
            strip (firmware_findings, read live from the latest runs).
  qualify   run_qualify on the host-bearing devices Discover passed. Each one's Deploy
            gate (deploy.gate_why) that is not open is a blocker.
  deploy    run_deploy. Targets are the hosts with no Discover blocker NOW whose gate is
            open NOW, with the waivers sent at that click (recorded by run_deploy). A
            device still blocked is left out and named in the Run's `out`; rack-wide
            only when none is left out. reinstall=no: installed nodes start at firstboot,
            never-installed ones at install (not a reinstall). reinstall=<order id>
            (typed up front): every target from install, except one this Run already
            installed (a Continue after an interrupted Deploy never wipes it twice).
  done      run_done, rack-wide (the customer CSVs list every device).

Lock rule: the caller holds the rack's stage lock for a Run's whole execution
(Discover through Qualify, then Deploy through Done), so nothing interleaves and the
apply_rackpost drain guard sees stage_running. A HELD Run holds no lock. The rack is
idle: the operator may re-run a device, waive, or deploy one from its drawer, and
Continue takes the lock again like any stage start (refused while another stage runs
or the daemon drains). Continue re-reads the gate at the click, never the hold's copy.

State is order.db's run_plan row (orderdb). A daemon that starts and finds a 'running'
Run marks it `interrupted` with the stage it was in and never resumes it; Continue
from there goes to Deploy (or only Done, when it was interrupted in Done). Cancel
stops a running Run (the stage's own Cancel, then the Run stops), and ends a held or
interrupted one.
"""
from flax_rungs.result import UNMEASURED, not_passed

from .deploy import (GateError, check_waivers, gate_why, installed_positions, run_deploy)
from .discover_ladder import DISCOVER_STEPS
from .done import run as run_done
from .firmware import FIRMWARE_RUNGS, run_firmware
from .qualify import run_qualify
from .stages import cancelled, run_discover

STAGE = "run"
STAGES = ("discover", "firmware", "qualify", "deploy", "done")
CONTINUABLE = ("held", "interrupted")


def _hosts(g) -> list:
    return [d for d in g.devices if d.endpoints.get("hosts")]


def _sw_keys(g, *, firmware=False) -> list[str]:
    return [f"_sw-{s.id}" for s in g.switches
            if s.endpoints or (not firmware and s.site_port)]


def _bad(lr, key, steps, stage) -> list[str]:
    """`key`'s steps of `stage` that block a Run: failed, not measured, interrupted, or
    (Firmware) an off-target version. A Firmware pair (x-checked, x-updated) is named
    once, by its first step."""
    out, seen = [], set()
    for st in steps:
        r = lr.get((key, st)) or {}
        pair = st.rsplit("-", 1)[0] if stage == "firmware" else st
        if pair in seen:
            continue
        if r.get("status") == "interrupted":
            why = f"{st} interrupted"
        elif (r.get("detail") or {}).get("phase") == "needs_update":
            why = f"{pair} needs update"
        elif r.get("verdict") in ("fail", UNMEASURED):
            why = not_passed(st, r["verdict"])
        else:
            continue
        seen.add(pair)
        out.append(f"{stage}: {why}")
    return out


def discover_blockers(lr, g) -> dict[str, list[str]]:
    """key (device pos or _sw-<id>) -> its Discover blockers, from the latest runs."""
    keys = [d.pos for d in g.devices] + _sw_keys(g)
    return {k: b for k in keys if (b := _bad(lr, k, DISCOVER_STEPS, "discover"))}


def firmware_findings(lr, keys) -> dict[str, list[str]]:
    """key -> its Firmware findings (off target, failed, not measured): reported, never
    a blocker."""
    return {k: b for k in keys if (b := _bad(lr, k, FIRMWARE_RUNGS, "firmware"))}


def deploy_choice(db, g, row: dict, waive=None) -> dict:
    """What a Run's Deploy does NOW (the gate at the click, never the hold's copy):
    {targets, out {pos: why}, firstboot [pos], from_phase, reinstall, waive}. Raises
    GateError on a malformed waiver."""
    lr = db.latest_runs()
    hosts = _hosts(g)
    w = check_waivers(g, waive, [d.pos for d in hosts])
    disc = discover_blockers(lr, g)
    targets, out = [], {}
    for d in hosts:
        if d.pos in disc:
            out[d.pos] = "; ".join(disc[d.pos])
        elif why := gate_why(lr, d, w.get(d.pos, {}).get("rungs", ())):
            out[d.pos] = why
        else:
            targets.append(d.pos)
    reinstall = g.order if row["options"].get("reinstall") else None
    if reinstall:
        # only what this Run installed already starts at firstboot (no second wipe)
        fb = db.ever_passed(("install",), after=row["run_after"]) & set(targets)
    else:
        fb = installed_positions(db, lr) & set(targets)
    return {"targets": targets, "out": out, "firstboot": sorted(fb),
            "from_phase": "firstboot" if targets and fb == set(targets) else "install",
            "reinstall": reinstall, "waive": {p: x for p, x in w.items() if p in targets}}


class _Run:
    def __init__(self, ctx, rid, on_stage):
        self.ctx, self.db, self.rid, self.on_stage = ctx, ctx.db, rid, on_stage

    def stage(self, name):
        self.db.set_run_plan(self.rid, state="running", stage=name)
        if self.on_stage:
            self.on_stage(name)
        self.db.add_event(STAGE, f"run {self.rid}: {name} ({STAGES.index(name) + 1}/{len(STAGES)})")

    def end(self, state, text):
        self.db.set_run_plan(self.rid, state=state)
        self.db.add_event(STAGE, f"run {self.rid} {text}")

    def stopped(self) -> bool:
        if cancelled(self.ctx):
            self.end("cancelled", "cancelled")
            return True
        return False

    def guard(self, fn):
        try:
            fn()
        except BaseException as e:      # the row never stays 'running' under a live daemon
            if self.db.run_plan(self.rid)["state"] == "running":
                self.end("interrupted" if isinstance(e, KeyboardInterrupt) else "failed",
                         f"{'interrupted' if isinstance(e, KeyboardInterrupt) else 'failed'}: "
                         f"{type(e).__name__}: {e}")
            raise


def start(db, who: str, *, reinstall: bool) -> int:
    """A new Run row (the caller holds the stage lock); a held one is superseded."""
    rid = db.start_run_plan(who, {"reinstall": bool(reinstall)})
    db.add_event(STAGE, f"run {rid} started by {who}"
                        + (" (reinstall confirmed: installed disks are wiped)" if reinstall
                           else " (no reinstall: installed nodes deploy from firstboot)"))
    return rid


def run(ctx, *, who: str = "?", reinstall: bool = False, on_stage=None) -> int:
    """A whole Run (the caller holds the rack's stage lock). -> its run_plan id."""
    if ctx.db.closed_at():
        raise RuntimeError("order is closed")
    x = _Run(ctx, start(ctx.db, who, reinstall=reinstall), on_stage)
    x.guard(lambda: _run(x))
    return x.rid


def _run(x: _Run):
    ctx, db, g = x.ctx, x.db, x.ctx.g
    held: dict[str, list[str]] = {}
    x.stage("discover")
    run_discover(ctx)
    if x.stopped():
        return
    held.update(discover_blockers(db.latest_runs(), g))
    fw = [d.pos for d in g.devices if d.pos not in held] + \
         [k for k in _sw_keys(g, firmware=True) if k not in held]
    x.stage("firmware")
    if fw:
        run_firmware(ctx, positions=None if not held else fw)
        if x.stopped():
            return
        if found := firmware_findings(db.latest_runs(), fw):
            db.add_event(STAGE, f"run {x.rid} firmware findings (report only, not held): "
                                + "; ".join(f"{k} ({', '.join(b)})" for k, b in found.items()))
    hosts = _hosts(g)
    ok = [d for d in hosts if not any(b.startswith("discover:") for b in held.get(d.pos, []))]
    x.stage("qualify")
    if ok:
        run_qualify(ctx, positions=None if len(ok) == len(hosts) else [d.pos for d in ok])
        if x.stopped():
            return
        lr = db.latest_runs()
        for d in ok:
            if why := gate_why(lr, d):
                held.setdefault(d.pos, []).append(f"deploy gate: {why}")
    if held:
        db.set_run_plan(x.rid, state="held", stage="deploy", held=held)
        db.add_event(STAGE, f"run {x.rid} held before deploy: "
                            + "; ".join(f"{k} ({', '.join(b)})" for k, b in held.items()))
        return
    _deploy_done(x, None, None)


def cont(ctx, rid: int, *, waive=None, who: str = "?", on_stage=None) -> None:
    """Continue a held / interrupted Run: Deploy (the gate at this click, with `waive`)
    then Done; only Done when it was interrupted in Done. The caller holds the lock."""
    row = ctx.db.run_plan(rid)
    if row is None or row["state"] not in CONTINUABLE:
        raise GateError(f"run {rid} is not held or interrupted")
    x = _Run(ctx, rid, on_stage)
    ctx.db.add_event(STAGE, f"run {rid}: continue by {who} (was {row['state']} in {row['stage']})")
    x.guard(lambda: _deploy_done(x, waive, who) if row["stage"] != "done" else _done(x))


def _deploy_done(x: _Run, waive, who):
    ctx, db, g = x.ctx, x.db, x.ctx.g
    row = db.run_plan(x.rid)
    ch = deploy_choice(db, g, row, waive)
    db.set_run_plan(x.rid, out=ch["out"])
    if not ch["targets"]:
        db.set_run_plan(x.rid, state="held", stage="deploy")
        db.add_event(STAGE, f"run {x.rid} held: no device passes the Deploy gate: "
                            + "; ".join(f"{p} ({y})" for p, y in ch["out"].items()))
        return
    x.stage("deploy")
    if ch["out"]:
        db.add_event(STAGE, f"run {x.rid}: left out of Deploy: "
                            + "; ".join(f"{p} ({y})" for p, y in ch["out"].items()))
    run_deploy(ctx, positions=ch["targets"] if ch["out"] else None,
               from_phase=ch["from_phase"], reinstall=ch["reinstall"], waive=ch["waive"] or None,
               who=who or row["who"],
               firstboot=ch["firstboot"] if ch["from_phase"] == "install" else ())
    if x.stopped():
        return
    _done(x)


def _done(x: _Run):
    x.stage("done")
    run_done(x.ctx)
    if x.stopped():
        return
    x.end("done", "done")
