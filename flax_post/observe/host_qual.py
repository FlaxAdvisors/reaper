# flax_post/observe/host_qual.py
"""The post Qualify+Done producer: a pure poller of the on-node agent.

Polls each booted blade's REST agent (qualclient), maps /status + /stages into
post_state.vars.qual, and captures each terminal stage's artifacts EXACTLY ONCE
into the durable post_artifact store (design §5.1/§6). Population-check (Task 5),
the Done tail (Task 6), and the re-run handshake (Task 7) build on this core.

Sole writer of post_state.vars.qual/pop/done + post_artifact. Never raises out of
run_once: a dead or misbehaving agent for one blade must not kill the loop.
"""
import logging

from .. import actions
from .. import population
from .. import state as _state
from ..qualclient import QualClient, QualUnreachable

log = logging.getLogger("flax-post.host_qual")

QUAL_MAP = {"pass": "done", "running": "cur", "pending": "pending",
            "fail": "fault", "skip": "done"}

_TERMINAL = {"pass", "fail"}
_BOOT_STEPS = ("pxe-in-logs", "live-iso-in-logs", "agent-reachable")


def boot_steps(reachable: bool) -> dict:
    """Engine boot-detection: a running agent proves PXE + live-ISO boot, so all
    three are 'pass' once reachable, else 'pending' (design: Global Constraints)."""
    v = "pass" if reachable else "pending"
    return {name: v for name in _BOOT_STEPS}


def _default_make_client(host_ip):
    return QualClient(f"http://{host_ip}:8087")   # agent port (Plan 2 fixes the literal)


# Engine-triggered Qualify start: a post node boots ONCE into `staylive` (so biosd/nicd
# can SSH-flash firmware); when Firmware completes (phase becomes "Qualify") the engine
# launches the on-node agent over SSH -- no reboot, no kernel-arg. Idempotent: the
# is-active guard skips a re-launch while the agent is already up (so a poll racing the
# startup window doesn't restart the battery). curl (not banghook) refreshes the payload
# to the latest build without touching /proc/cmdline's action.
_LAUNCH_SH = (
    "systemctl is-active --quiet flax-qual-agent && exit 0\n"
    "set -e\n"
    "cd /opt/flax/hook\n"
    "curl -sf http://bang/post.tgz -o post.tgz && tar xzf post.tgz\n"
    "./post.sh postautomate\n"
)


def _default_launch_agent(target) -> tuple:
    """SSH the booted node and start the qual agent (post.sh's postautomate branch).
    Reuses biosd's host-cred loader + ssh runner (root via passwordless sudo). The
    launch (curl+tar+systemd-run) is quick, so a tight 30s timeout bounds how long a
    flaky node can stall the (single-threaded) poll loop; rc/output is logged because
    this is the SOLE automated path into Qualify -- a silent bad-creds/unreachable-bang
    failure would otherwise strand nodes in phase=='Qualify' with no trail.
    FAST-FOLLOW: offload to a bounded thread pool (cf. fwd) if a large firmware batch
    completes at once; and skip auto-relaunch of a node whose last verdict was 'fail'
    (leave that to the manual restart handshake)."""
    from ..biosd import creds as _creds, driver as _driver
    user, pw = _creds.load_host_creds()
    rc, out = _driver.run_over_ssh(user, pw, target["host_ip"], _LAUNCH_SH, timeout=30)
    if rc == 0:
        log.info("qual agent launched on %s (%s)", target.get("port"), target["host_ip"])
    else:
        log.warning("qual agent launch on %s (%s) rc=%s: %s",
                    target.get("port"), target["host_ip"], rc, (out or "").strip()[:400])
    return rc, out


def population_check(dump, profile) -> dict:
    """green = all profile rules matched, red = any missing, grey = no dump/profile."""
    if not dump or not profile:
        return {"profile": profile, "verdict": "grey", "failed_rules": []}
    rules = population.load_profile(profile)
    res = population.evaluate(rules, dump)
    failed = [r["rule"] for r in res["results"] if not r["ok"]]
    return {"profile": profile, "verdict": "green" if res["ok"] else "red",
            "failed_rules": failed}


def _read_profile_for(target, store) -> "str | None":
    """The population profile in effect: the blade's pop_override, else the global
    order default from post_settings.population (design §5.6)."""
    live = store.read_state().get(target["port"], {}) if hasattr(store, "read_state") else {}
    override = live.get("pop_override")
    if override:
        return override
    return store.read_settings().get("population") if hasattr(store, "read_settings") else None


def qualify_verdict(status, pop) -> "str | None":
    """pass = node battery passed AND population green; fail = either failed; else None."""
    node = (status or {}).get("verdict")
    popv = (pop or {}).get("verdict")
    if node == "fail" or popv == "red":
        return "fail"
    if node == "pass" and popv == "green":
        return "pass"
    return None


def run_done(target, verdict, *, identify=actions.run_identify, power=actions.run_power) -> dict:
    """On pass: identify LED force-on ('pull me') then power off. On fail: nothing
    (leave the node powered for inspection, design §9/§10)."""
    if verdict != "pass":
        return {"verdict": verdict}
    identify(target["bmc_ip"], "force")
    power(target["bmc_ip"], "off", blocked=False)
    return {"identify": "done", "power_off": "done", "verdict": "pass"}


def poll_target(target, *, make_client=_default_make_client, store=_state,
                launch_agent=None) -> dict:
    """Poll one blade; write vars.qual; capture terminal-stage artifacts once. When the
    agent is unreachable but Firmware is done (phase == 'Qualify'), launch_agent (if
    given) SSH-starts the agent so the next pass can poll it."""
    client = make_client(target["host_ip"])
    try:
        health = client.health()
    except QualUnreachable:
        # A node that already PASSED was intentionally powered off by the Done tail;
        # don't regress its green tile (phase + Qualify bar) to unreachable/pending.
        live = store.read_state().get(target["port"], {}) if hasattr(store, "read_state") else {}
        if (live.get("done") or {}).get("verdict") == "pass":
            return live.get("qual") or {}
        # Firmware complete, agent not up yet -> trigger the postautomate launch (once;
        # the launch script no-ops if it's already active). Never let a launch failure
        # kill the poll.
        if launch_agent is not None and target.get("phase") == "Qualify":
            try:
                launch_agent(target)
            except Exception:
                log.exception("agent launch trigger failed for %s", target.get("port"))
        qual = {"agent": {"reachable": False},
                "steps": {k: {"status": v} for k, v in boot_steps(False).items()}}
        store.set_state(target["port"], qual=qual)
        return qual
    run_id = health.get("run_id")
    status = client.status()
    stages = client.stages()
    steps = boot_steps(True)
    for s in stages:
        steps[s["name"]] = {"status": s["status"], "started": s.get("started"),
                            "ended": s.get("ended")}
    # boot_steps returns strings; normalize to the {"status": ...} shape used by stages
    for b in _BOOT_STEPS:
        if isinstance(steps[b], str):
            steps[b] = {"status": steps[b]}
    # capture-on-completion: for each terminal stage, store artifacts not yet stored
    for s in stages:
        if s["status"] in _TERMINAL:
            existing = {a["name"] for a in store.list_artifacts(target["bmc_mac"], run_id, s["name"])}
            detail = client.stage(s["name"])
            for art in detail.get("artifacts", []):
                if art["name"] not in existing:
                    text = client.stage_artifact(s["name"], art["name"])
                    store.write_artifact(target["bmc_mac"], run_id, s["name"], art["name"],
                                         art.get("kind", "raw"), text,
                                         serial=target.get("serial"),
                                         order_no=target.get("order_no"), nbytes=art.get("bytes"))
            steps[s["name"]]["summary"] = detail.get("summary", {})
            steps[s["name"]]["artifacts"] = detail.get("artifacts", [])
    # inventory dump (the macinv digest) -> population-check (engine-owned, design §4)
    dump = store.get_artifact(target["bmc_mac"], run_id, "inventory", "macinv") \
        if hasattr(store, "get_artifact") else None
    profile = _read_profile_for(target, store)
    pop = population_check(dump, profile)
    steps["population-check"] = {"status": {"green": "pass", "red": "fail", "grey": "pending"}[pop["verdict"]]}
    store.set_state(target["port"], pop=pop)
    qual = {"run_id": run_id, "agent": {"reachable": True, "ver": health.get("agent_ver")},
            "overall": status, "steps": steps}
    verdict = qualify_verdict(status, pop)
    if verdict is not None:
        done = run_done(target, verdict)
        store.set_state(target["port"], done=done)
        qual["done"] = done
    store.set_state(target["port"], qual=qual)
    return qual


def restart_target(target, *, make_client=_default_make_client, store=_state) -> dict:
    """Re-run handshake (design §5.2): restart -> purge old run's evidence + clear
    live qual/pop/done -> ACK (the commit point) -> agent starts the new run."""
    client = make_client(target["host_ip"])
    try:
        ids = client.restart()
    except QualUnreachable:
        return {"ok": False, "reason": "unreachable"}
    old_run = ids.get("old_run_id")
    store.purge_run(target["bmc_mac"], old_run)
    store.set_state(target["port"], qual={}, pop={}, done={})
    client.restart_ack()
    return {"ok": True, "new_run_id": ids.get("new_run_id")}


def run_once(targets, *, make_client=_default_make_client, store=_state,
             launch_agent=_default_launch_agent) -> None:
    """Poll every booted target; a failure on one never aborts the pass."""
    for target in targets:
        try:
            poll_target(target, make_client=make_client, store=store, launch_agent=launch_agent)
        except Exception:
            log.exception("host_qual poll failed for %s", target.get("port"))
