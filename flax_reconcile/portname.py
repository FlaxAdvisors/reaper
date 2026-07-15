"""Port-name canonicalization for flax-reconcile.

Ports exist in two forms across the flax stack:
  - internal short form ``et6b1`` (geometry, observe_state, devices.port as
    written by flax-discover); and
  - Arista canonical ``Ethernet6/1`` (switch_facts.ports keys, desired_port,
    what the AristaEAPI driver's CLI requires).

Per spec §6 the reconcile flow is Arista-canonical end to end: the flap, the
intentional_flap sentinel, and the steered_ports skip comparison must all use
the Arista form so observe (which also canonicalizes) sees the same shape.

``to_arista`` is the inverse of switchportrecond.arista_port_to_internal and
mirrors flax_classify.feeder / flax_observe.port_worker's ``_internal_to_arista``.
It is idempotent on already-canonical names and a passthrough for non-Arista
port names (e.g. Cumulus ``swp6``), so it is always safe to apply at a boundary.

Deliberately self-contained: no cross-import between service packages.
"""
import re


def to_arista(port: str) -> str:
    """et6b1 → Ethernet6/1; Ethernet6/1 and swp6 returned unchanged."""
    m = re.match(r"^et(\d+)b(\d+)$", port)
    if not m:
        return port
    return f"Ethernet{m.group(1)}/{m.group(2)}"


def to_internal(port: str) -> str:
    """Ethernet6/1 (or Et6/1) → et6b1; et6b1 and swp6 returned unchanged.

    Inverse of to_arista. Used to key the BMC-FW claim sentinel on the
    slash-free internal form regardless of whether the request arrived in
    Arista (auto path) or internal (operator path) form. Idempotent on
    already-internal names; passthrough for non-Arista names (e.g. swp6)."""
    m = re.match(r"^Et(?:hernet)?(\d+)/(\d+)$", port)
    if not m:
        return port
    return f"et{m.group(1)}b{m.group(2)}"
