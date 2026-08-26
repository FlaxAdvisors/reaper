"""VLAN-steer decision + site no-steer list.

compute_steers is pure: classify's desired_port vs live switch_facts, gated by
the access-port check AND the site no-steer list (the independent hard floor;
classify applies the same list, so a downstream-uplink write needs two failures).
Enforcement (set_access_vlan + flap + sentinel + log) is in cycle.py.
"""
import json
import logging

log = logging.getLogger("flax-reconcile.steer")


def load_no_steer(path: str) -> set:
    """Parse /etc/flax/no-steer-ports.json -> {(switch, port)}. Missing file =>
    empty set (no exclusions). Malformed => fatal (fail loud on a safety file)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        log.info("no %s; no ports excluded from steering", path)
        return set()
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"malformed no-steer file {path}: {e}") from e
    out = set()
    for e in data:
        out.add((e["switch"], e["port"]))
    return out


def compute_steers(desired_ports: list[dict], sf_ports: dict,
                   no_steer: set) -> list[dict]:
    """Return per-port steer/refuse decisions for ports whose desired VLAN
    differs from their current access VLAN. Ports already on the right VLAN
    produce nothing."""
    out = []
    for d in desired_ports:
        key = (d["switch"], d["port"])
        sf = sf_ports.get(key, {})
        current = sf.get("access_vid")
        if d["desired_vid"] == current:
            continue
        if key in no_steer:
            action, reason = "refuse", "no_steer_listed"
        elif sf.get("mask") != "access":
            action, reason = "refuse", "non_access_port"
        else:
            action, reason = "steer", None
        out.append({"switch": d["switch"], "port": d["port"],
                    "desired_vid": d["desired_vid"], "current_vid": current,
                    "action": action, "reason": reason})
    return out
