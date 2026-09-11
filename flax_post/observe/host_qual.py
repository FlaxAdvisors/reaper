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
import os
import time

from .. import actions
from .. import population
from .. import state as _state
from ..qualclient import QualClient, QualUnreachable

log = logging.getLogger("flax-post.host_qual")

# Debounce the qualify-agent launch. A node that finished Firmware sits at
# phase=='Qualify'; host_qual SSH-launches the on-node agent, but that launch
# (post.sh postautomate -> `systemctl stop` + `systemd-run`) is NOT idempotent:
# firing it every poll while the agent is still coming up stomps the starting
# agent, so it never persists. It also must not race Firmware's own resets/
# reboots. So launch at most once per cooldown per node and let it settle;
# a failed launch (host mid-reboot) simply retries after the cooldown.
LAUNCH_COOLDOWN_S = int(os.environ.get("FLAX_POST_QUAL_LAUNCH_COOLDOWN", "120"))

QUAL_MAP = {"pass": "done", "running": "cur", "pending": "pending",
            "fail": "fault", "skip": "done"}

# skip is terminal too: its summary.reason (e.g. fio's "no physical storage
# media") is what the tile renders as the step note. Skips emit no artifacts.
_TERMINAL = {"pass", "fail", "skip"}

# The Done tail reads chassis power back after the off command (spec 2026-09-11
# §2.2): 'power_off: done' is only ever written against a read that says off.
POWER_OFF_VERIFY_S = int(os.environ.get("FLAX_POST_POWER_OFF_VERIFY", "30"))
POWER_OFF_POLL_S = 5


def agent_step(reachable: bool) -> dict:
    """The engine's own Qualify step: a running agent answers /health. The
    boot markers moved to the Discover ladder (observe/ladder.py)."""
    return {"agent-reachable": {"status": "pass" if reachable else "pending"}}


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
    # The slot ladder launches within seconds of sshd answering, which can be
    # BEFORE the ISO's banghook service has created /opt/flax/hook (et8b4,
    # 2026-09-11: `cd: /opt/flax/hook: No such file or directory`). The launch
    # fetches its own payload, so it needs nothing from the hook but the dir.
    "mkdir -p /opt/flax/hook && cd /opt/flax/hook\n"
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


def _power_reading(res) -> str:
    """'on' | 'off' | 'unreadable' from a run_power(status) result."""
    res = res or {}
    out = (res.get("output") or "").lower()
    if not res.get("ok"):
        return "unreadable"
    return "on" if "is on" in out else ("off" if "is off" in out else "unreadable")


def run_done(target, verdict, *, identify=actions.run_identify, power=actions.run_power,
             sleep=time.sleep) -> dict:
    """On pass: identify LED force-on ('pull me'), power off, then READ POWER BACK for
    up to POWER_OFF_VERIFY_S. power_off is 'done' only when a read says off; otherwise
    'fault' with power_off_reason still_on|unreadable. On fail: nothing (leave the
    node powered for inspection, design §9/§10)."""
    if verdict != "pass":
        return {"verdict": verdict}
    idf = identify(target["bmc_ip"], "force")
    power(target["bmc_ip"], "off", blocked=False)
    reading = "unreadable"
    for _ in range(max(1, POWER_OFF_VERIFY_S // POWER_OFF_POLL_S)):
        reading = _power_reading(power(target["bmc_ip"], "status", blocked=False))
        if reading == "off":
            break
        sleep(POWER_OFF_POLL_S)
    done = {"identify": "done" if (idf or {}).get("ok") else "fault",
            "power_off": "done" if reading == "off" else "fault",
            "verdict": "pass"}
    if reading != "off":
        done["power_off_reason"] = "still_on" if reading == "on" else "unreadable"
    return done


def build_result(target, live, qual, pop, done, now=time.time) -> dict:
    """The durable record of one finished run (spec 2026-09-11 §3). Artifact lists
    are dropped from the steps: post_artifact is the store for those."""
    steps = {}
    for name, rec in ((qual or {}).get("steps") or {}).items():
        rec = rec or {}
        steps[name] = {"status": rec.get("status"), "summary": rec.get("summary") or {}}
    live = live or {}
    return {"run_id": (qual or {}).get("run_id"), "verdict": (done or {}).get("verdict"),
            "finished_at": int(now()), "order_no": target.get("order_no"),
            "port": target.get("port"), "serial": target.get("serial"),
            "fw": {"bmc": live.get("fw_bmc") or {}, "bios": live.get("fw_bios") or {},
                   "nic": live.get("fw_nic") or {}},
            "qual": {"overall": (qual or {}).get("overall") or {}, "steps": steps},
            "pop": pop or {}, "done": done or {}}


def poll_target(target, *, make_client=_default_make_client, store=_state,
                launch_agent=None, now=time.time, ping=None, console_reader=None) -> dict:
    """Poll one blade; write vars.qual; capture terminal-stage artifacts once.

    Guard order (spec 2026-09-11 post-slot-ladder §10): a row with a verdict is
    latched and costs nothing; a powered-off row costs nothing; then `ping`
    (when given) must answer before /health is tried, so a leased-but-dead IP
    never burns an ARP timeout. When the agent is unreachable but Firmware is
    done (phase == 'Qualify'), launch_agent (if given) SSH-starts it, debounced
    to once per LAUNCH_COOLDOWN_S."""
    live = store.read_state().get(target["port"], {}) if hasattr(store, "read_state") else {}
    if (live.get("done") or {}).get("verdict") is not None:
        return live.get("qual") or {}
    if live.get("power_on") == "off":
        return live.get("qual") or {}
    pingable = True if ping is None else bool(ping(target["host_ip"]))
    client = make_client(target["host_ip"]) if pingable else None
    try:
        if not pingable:
            raise QualUnreachable("no ping")
        health = client.health()
    except QualUnreachable:
        # A node with a verdict is latched (spec 2026-09-11 §2): a passed one was
        # powered off by the Done tail, a failed one may be powered off by the
        # operator. Either way its qualify evidence (which step failed, the run
        # id) stays on the tile; and a failed blade is not relaunched until the
        # power lane clears the latch on the next off->on.
        # Firmware complete, agent not up yet -> trigger the postautomate launch,
        # debounced: skip if we launched within LAUNCH_COOLDOWN_S (the launch is not
        # idempotent and stomps a still-starting agent; Firmware resets must settle).
        # Record the attempt time BEFORE launching so a failed launch (host mid-reboot)
        # still backs off a full cooldown. Never let a launch failure kill the poll.
        if pingable and launch_agent is not None and target.get("phase") == "Qualify":
            last = (live.get("launch_at") or 0) if isinstance(live, dict) else 0
            t = now()
            if t - last >= LAUNCH_COOLDOWN_S:
                store.set_state(target["port"], launch_at=t)
                try:
                    launch_agent(target)
                except Exception:
                    log.exception("agent launch trigger failed for %s", target.get("port"))
            else:
                log.debug("qual launch debounced for %s (%.0fs into %ss cooldown)",
                          target.get("port"), t - last, LAUNCH_COOLDOWN_S)
        qual = {"agent": {"reachable": False}, "steps": agent_step(False)}
        store.set_state(target["port"], qual=qual)
        return qual
    run_id = health.get("run_id")
    status = client.status()
    stages = client.stages()
    steps = agent_step(True)
    for s in stages:
        steps[s["name"]] = {"status": s["status"], "started": s.get("started"),
                            "ended": s.get("ended")}
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
        # Once per run: the Done tail (identify + power off) and the durable
        # post_node record both happen the FIRST time this run reaches a verdict.
        # Before this guard the tail re-ran on every poll while the agent was
        # still answering after a pass.
        already = ((live.get("done") or {}).get("verdict") is not None
                   and (live.get("qual") or {}).get("run_id") == run_id)
        if already:
            done = live.get("done")
        else:
            done = run_done(target, verdict)
            store.set_state(target["port"], done=done)
            if console_reader is not None:
                text = None
                try:
                    text = console_reader(target.get("bmc_ip"))
                except Exception:
                    log.exception("console capture read failed for %s", target.get("port"))
                if text:
                    store.write_artifact(target["bmc_mac"], run_id, "console", "sol.txt", "raw",
                                         text, serial=target.get("serial"),
                                         order_no=target.get("order_no"))
                    steps["console"] = {"status": "pass"}
                else:
                    steps["console"] = {"status": "fail", "summary": {"reason": "no sol capture"}}
                qual["steps"] = steps
            if hasattr(store, "record_result"):
                try:
                    store.record_result(target["bmc_mac"],
                                        build_result(target, live, qual, pop, done),
                                        serial=target.get("serial"),
                                        order_no=target.get("order_no"),
                                        last_port=target["port"])
                except Exception:
                    log.exception("record_result failed for %s", target.get("port"))
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
