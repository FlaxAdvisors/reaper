"""GC orphaned post_state rows (blade gone + unreserved + not flashing).

Pure `plan_state_gc` decides; `gc_post_state` applies. An anti-flap latch
(`vars.gc_gone_since`) holds a candidate POST_STATE_GC_GRACE_SECS before delete,
cleared if the blade/reservation returns. post_node is never touched.
"""
import collections
import datetime
import logging
import os

from .. import consume, geometry, queries, state

log = logging.getLogger("flax-post.observe.gc")

# Fallback default ONLY; the live post switch is derived from geometry (see
# ipmi._post_switch) -- hardcoding rabbit-edam made gc skip braintree (its post
# switch is rabbit-lorax) as "switch_unreachable".
SWITCH = "rabbit-edam"
FLASH_PHASES = {"checking", "flashing", "monitoring", "activating"}
GRACE_SECS = int(os.environ.get("POST_STATE_GC_GRACE_SECS", "300"))

StateGcPlan = collections.namedtuple(
    "StateGcPlan", ["deletes", "latch_writes", "latch_clears"])


def _norm(m):
    return str(m).strip().lower()


def plan_state_gc(states, reserved_ports, switch_facts, now, grace_secs):
    """Pure GC decision. See module docstring.

    states: {port: rec} from state.read_state() (rec carries bmc_mac,
      gc_gone_since, fw_bmc). reserved_ports: set of ports with a live
      source=post reservation. switch_facts: {arista_port: {macs: [...]}}.
    Returns StateGcPlan(deletes=[port], latch_writes={port: iso}, latch_clears=[port]).
    """
    deletes, latch_writes, latch_clears = [], {}, []
    for port, rec in states.items():
        fact = switch_facts.get(geometry.to_arista(port))
        if fact is None:
            # port not visible in switch_facts -> unknown; never GC on missing data
            if rec.get("gc_gone_since"):
                latch_clears.append(port)
            continue
        fw = rec.get("fw_bmc") or {}
        flashing = isinstance(fw, dict) and fw.get("phase") in FLASH_PHASES
        reserved = port in reserved_ports
        fdb = {_norm(m) for m in (fact.get("macs") or [])}
        bmc = rec.get("bmc_mac")
        bmc_present = bool(bmc) and _norm(bmc) in fdb
        candidate = (not reserved) and (not flashing) and (not bmc_present)
        since = rec.get("gc_gone_since")
        if candidate:
            if not since:
                latch_writes[port] = now.isoformat()
            else:
                try:
                    elapsed = (now - datetime.datetime.fromisoformat(since)).total_seconds()
                except (TypeError, ValueError):
                    elapsed = 0.0
                if elapsed >= grace_secs:
                    deletes.append(port)
        elif since:
            latch_clears.append(port)
    return StateGcPlan(deletes, latch_writes, latch_clears)


def gc_post_state(now=None, states=None, reserved_ports=None, switch_facts=None,
                  grace_secs=GRACE_SECS, switch=None) -> dict:
    """Read state + reservations + switch_facts, plan, apply. Switch-unreachable
    (empty switch_facts) -> skip entirely."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if states is None:
        states = state.read_state()
    if reserved_ports is None:
        reserved_ports = {d["port"] for d in queries.post_devices() if d.get("port")}
    if switch_facts is None:
        if switch is None:
            from . import ipmi
            switch = ipmi._post_switch()
        switch_facts = consume.switch_ports(switch)
    if not switch_facts:
        return {"deleted": 0, "latched": 0, "cleared": 0, "skipped": "switch_unreachable"}
    plan = plan_state_gc(states, reserved_ports, switch_facts, now, grace_secs)
    for port in plan.deletes:
        state.delete_state(port)
    for port, iso in plan.latch_writes.items():
        state.set_state(port, gc_gone_since=iso)
    for port in plan.latch_clears:
        state.set_state(port, gc_gone_since=None)
    return {"deleted": len(plan.deletes), "latched": len(plan.latch_writes),
            "cleared": len(plan.latch_clears)}
