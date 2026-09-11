# flax_post/observe/ladder.py
"""The per-slot readiness ladder: a PURE rung machine (spec 2026-09-11
post-slot-ladder §4/§5). No I/O here. The worker (observe/worker.py) gathers
the one piece of evidence `evidence_needed` asks for, calls `advance`, then
performs the returned actions and persists the returned slice.

Fault-and-stop: a budget expiry writes ladder.fault and the machine emits no
further actions until the slice is reset — by a human off-to-on (the IPMI
lane writes human_reset) or by the hold-file gesture (touch, then remove).
"""
import copy
import os

from ..blades import BOOT_MARKER_RUNGS, RUNGS

_DEFAULT_BUDGET_S = {
    "power-on": 60, "tftp-seen": 600, "ipxe-seen": 120, "host-leased": 120,
    "live-iso-seen": 300, "host-pinged": 600, "host-ssh": 120, "fw-gates": 600,
    "agent-reachable": 180,
}
POWER_COOLDOWN_S = int(os.environ.get("FLAX_POST_LADDER_POWER_COOLDOWN_S", "900"))
_MARK_KEY = {"tftp-seen": "tftp", "ipxe-seen": "ipxe", "live-iso-seen": "iso"}
_EVIDENCE = {"tftp-seen": "tftp", "ipxe-seen": "ipxe", "live-iso-seen": "iso",
             "host-pinged": "ping", "host-ssh": "ssh", "agent-reachable": "agent"}
_NEXT = {r: RUNGS[i + 1] for i, r in enumerate(RUNGS[:-1])}


def budget_s(rung):
    """Seconds allowed on `rung`, None for rungs that never time out."""
    env = os.environ.get("FLAX_POST_LADDER_%s_S" % rung.replace("-", "_").upper())
    if env:
        return int(env)
    return _DEFAULT_BUDGET_S.get(rung)


def new_ladder(now) -> dict:
    return {"rung": "bmc-pinged", "since": now, "power_on_at": None,
            "power_on_pending": False, "marks": {}, "fault": None, "probe_calls": {}}


def human_reset(now) -> dict:
    """A human powered the blade on: restart as if first seen, past power-on."""
    lad = new_ladder(now)
    lad.update(rung="tftp-seen", power_on_at=now, human_power_on=True)
    return lad


def fault(ladder, rung, reason, now) -> dict:
    lad = copy.deepcopy(ladder)
    lad["fault"] = {"rung": rung, "reason": reason, "at": now}
    lad["power_on_pending"] = False
    return lad


def evidence_needed(ladder):
    if not ladder or ladder.get("fault"):
        return None
    return _EVIDENCE.get(ladder.get("rung"))


IDLE_INTERVAL_S = 15


def interval_s(ladder) -> int:
    """Worker cadence for a step that did something: 2s while a rung is in
    flight, 10s while polling the battery, 15s when idle, faulted or done.

    A step that neither acted nor changed the slice is idle whatever its rung
    (a slot parked at power-on because it is off/latched/held/not allowlisted)
    and the worker uses IDLE_INTERVAL_S directly — see worker.iterate_once."""
    if not ladder or ladder.get("fault"):
        return IDLE_INTERVAL_S
    rung = ladder.get("rung")
    if rung == "qualify":
        return 10
    if rung in ("done", "bmc-pinged"):
        return IDLE_INTERVAL_S
    return 2


def _pass(lad, next_rung, now):
    lad["rung"] = next_rung
    lad["since"] = now


def _over_budget(lad, rung, since, now):
    b = budget_s(rung)
    return b is not None and (now - since) > b


def advance(ladder, snap, evidence, now):
    """One step. Returns (new_ladder, actions). Never mutates its inputs."""
    lad = copy.deepcopy(ladder) if ladder else new_ladder(now)
    ev = evidence or {}
    acts = []

    if lad.get("fault"):
        if snap.get("hold"):
            lad["hold_seen"] = True
        elif lad.get("hold_seen"):
            return new_ladder(now), ["unclaim"]
        return lad, []

    if lad.pop("human_power_on", None):
        acts += ["sol-mark:operator", "claim"]

    rung = lad["rung"]

    if rung == "bmc-pinged":
        if snap.get("bmc_pinged"):
            _pass(lad, "power-on", now)
            acts.append("probe-fwd")
        return lad, acts

    if rung == "power-on":
        power = snap.get("power_on")
        if lad.get("power_on_pending"):
            if power == "on":
                lad["power_on_pending"] = False
                _pass(lad, "tftp-seen", now)
            elif _over_budget(lad, "power-on", lad["power_on_at"], now):
                lad = fault(lad, "power-on", "not on after %ds" % budget_s("power-on"), now)
                acts.append("unclaim")
            return lad, acts
        if power == "on":
            # Already on when first seen (rack in flight, observe restart): the
            # boot markers for this boot are unknowable, so skip them.
            lad["marks"]["skipped"] = "power already on"
            lad["power_on_at"] = lad.get("power_on_at") or now
            # A latched blade that happens to be on is finished, not booting:
            # walking on would ssh the host, fire the firmware probes and then
            # fault at agent-reachable with no agent left to reach.
            _pass(lad, "done" if snap.get("verdict") is not None else "host-pinged", now)
            return lad, acts
        if power != "off":
            return lad, acts
        if snap.get("verdict") is not None or not snap.get("allowed") \
                or snap.get("hold") or snap.get("fw_flashing"):
            return lad, acts
        last_attempt = lad.get("last_power_attempt")
        if last_attempt is not None and now - last_attempt < POWER_COOLDOWN_S:
            return lad, acts
        lad.update(power_on_pending=True, power_on_at=now, last_power_attempt=now)
        return lad, acts + ["sol-mark:power-on", "claim", "power-on"]

    if rung in BOOT_MARKER_RUNGS:
        key = _MARK_KEY[rung]
        ts = ev.get(key)
        if ts:
            lad["marks"][key] = ts
            _pass(lad, _NEXT[rung], now)
            return lad, acts
        since = lad["power_on_at"] if rung == "tftp-seen" else lad["since"]
        if _over_budget(lad, rung, since, now):
            lad = fault(lad, rung, "no %s after %ds" % (key, budget_s(rung)), now)
            acts.append("unclaim")
        return lad, acts

    if rung == "host-leased":
        if snap.get("host_leased"):
            _pass(lad, "live-iso-seen", now)
        elif _over_budget(lad, rung, lad["since"], now):
            lad = fault(lad, rung, "no host lease after %ds" % budget_s(rung), now)
            acts.append("unclaim")
        return lad, acts

    if rung == "host-pinged":
        if ev.get("ping"):
            lad["marks"]["ping"] = now
            _pass(lad, "host-ssh", now)
        elif _over_budget(lad, rung, lad["since"], now):
            lad = fault(lad, rung, "no ping after %ds" % budget_s(rung), now)
            acts.append("unclaim")
        return lad, acts

    if rung == "host-ssh":
        if ev.get("ssh"):
            lad["marks"]["ssh"] = now
            _pass(lad, "fw-gates", now)
            acts += ["unclaim", "probe-biosd", "probe-nicd"]
        elif _over_budget(lad, rung, lad["since"], now):
            lad = fault(lad, rung, "no ssh after %ds" % budget_s(rung), now)
            acts.append("unclaim")
        return lad, acts

    if rung == "fw-gates":
        if snap.get("fw_gates"):
            _pass(lad, "agent-reachable", now)
            acts.append("poll-agent")
        elif _over_budget(lad, rung, lad["since"], now):
            lad = fault(lad, rung, "firmware gate not passed after %ds" % budget_s(rung), now)
        return lad, acts

    if rung == "agent-reachable":
        if ev.get("agent"):
            _pass(lad, "qualify", now)
            return lad, acts + ["poll-agent"]
        # launch_at is never cleared: a row that reaches this rung without a
        # fresh launch (the non-allowlisted slot, or a re-run) carries the
        # launch of a previous boot. Only a launch at or after this rung
        # started moves the clock; otherwise the rung's own `since` governs.
        launch_at = snap.get("launch_at")
        since = launch_at if (launch_at is not None and launch_at >= lad["since"]) else lad["since"]
        if _over_budget(lad, rung, since, now):
            lad = fault(lad, rung, "agent not reachable %ds after launch" % budget_s(rung), now)
            return lad, acts
        return lad, acts + ["poll-agent"]

    if rung == "qualify":
        if snap.get("verdict") is not None:
            _pass(lad, "done", now)
            return lad, acts
        return lad, acts + ["poll-agent"]

    return lad, acts          # done
