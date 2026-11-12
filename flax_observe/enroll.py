"""Lab-enrollment scope for flax-observe (post-world dynamic enrollment).

Pure, DB-free helpers that compute which access ports on the lab's rabbit
switch(es) flax-observe should enroll device-discovery workers for. The set is
derived live from the injected SwitchFactsCache (every mask=access port minus
no-steer), decoupled from the Triage geometry.json subset.

Port-form contract: switch_facts.ports keys are Arista canonical long form
(Ethernet10/2); observe/geometry/observe_state use internal short form
(et10b2). no-steer-ports.json entries are Arista form. We compare Arista-vs-
Arista (before converting) and return internal short form.

load_no_steer is reused from flax_classify.vlan_policy (the flax-control image
bundles both packages), so observe and classify read the identical site file.
"""
import logging

from flax_classify.vlan_policy import load_no_steer  # noqa: F401  (re-export)

from .port_worker import _arista_to_internal


log = logging.getLogger("flax-observe.enroll")


def dynamic_access(cache, switches, no_steer):
    """Access ports to enroll across the rabbit switch(es), internal short form.

    For each switch name in `switches`, read its ports from `cache` (via
    cache.ports_for(switch) -> {arista_port: fact}); for every port whose
    `mask == "access"` and whose (switch, arista_port) is NOT in `no_steer`,
    convert the Arista key to internal short form and add (switch,
    internal_port) to the result. Returns a set of (switch, internal_port)
    tuples (deduped).

    Pure over the injected cache. A switch missing from the cache yields no
    ports (skip, no crash). `no_steer` is the {(switch, port)} set in Arista
    form (flax_classify.vlan_policy.load_no_steer's shape); membership is
    checked before conversion so it stays Arista-vs-Arista.
    """
    out: set = set()
    for switch in switches:
        ports = cache.ports_for(switch) or {}
        for arista_port, fact in ports.items():
            if not isinstance(fact, dict):
                continue
            if fact.get("mask") != "access":
                continue
            if (switch, arista_port) in no_steer:
                continue
            out.add((switch, _arista_to_internal(arista_port)))
    return out
