"""Read-side joins of the EXISTING flax services (docs/Post-UI-Design.md §3.1).

Consumes switch_facts (flax-switch-sense, rabbit-edam) + reservations/leases
(queries.post_devices) and produces a per-port view of the Discover-phase facts.
No BMC contact, no switch writes — pure consumption.
"""
from . import geometry
from .db import get_pool

_SWITCH_SQL = "SELECT switch, ports FROM switch_facts WHERE switch = %s AND reachable = true"


def switch_ports(switch: str) -> dict:
    """{arista_port: fact} for `switch` when reachable, else {}."""
    with get_pool().connection() as conn:
        rows = conn.execute(_SWITCH_SQL, (switch,)).fetchall()
    for _sw, ports in rows:
        if isinstance(ports, dict):
            return ports
    return {}


def _link_value(linkstate) -> str:
    return "link" if str(linkstate).lower() in ("link", "connected", "up") else "nolink"


def _mac_seen(fact: dict, mac: str) -> bool:
    if not mac:
        return False
    target = mac.lower()
    return any(str(m).lower() == target for m in (fact.get("macs") or []))


def consumed_by_port(devices: list, switch_facts: dict, switch: str) -> dict:
    """Per-port Discover facts from reservations + switch_facts (pure)."""
    by_port: dict[str, dict] = {}
    for d in devices:
        if d.get("switch") != switch or not d.get("port"):
            continue
        by_port.setdefault(d["port"], {})[d.get("kind")] = d

    out: dict[str, dict] = {}
    for port, kinds in by_port.items():
        fact = switch_facts.get(geometry.to_arista(port)) or {}
        link = _link_value(fact.get("link")) if fact else "nolink"
        rec = {"link": link, "bmc_mac": None, "host_mac": None}
        for kind in ("bmc", "host"):
            dev = kinds.get(kind)
            rec[f"{kind}_reserved"] = dev is not None
            rec[f"{kind}_mac"] = dev.get("mac") if dev else None
            rec[f"{kind}_mac_seen"] = bool(dev) and link == "link" and _mac_seen(fact, dev.get("mac"))
            rec[f"{kind}_ip"] = (dev.get("lease_ip") or dev.get("reservation_ip")) if dev else None
            rec[f"{kind}_leased"] = bool(dev and dev.get("leased"))
        out[port] = rec
    return out
