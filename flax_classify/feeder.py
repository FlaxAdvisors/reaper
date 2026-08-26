"""Join observe_state + switch_facts + devices into per-mac classification targets.

Bridges the gap between flax-observe's chassis-shaped output (one row per
enrolled port, with bmc_mac/nic_mac in `resolved`) and flax-classify's
mac-shaped output (one row per reservable MAC). bmc/host targets stay sourced
from observe_state so reservations survive flax-discover being down (family
then defaults to 'unknown'); family is joined in from the devices table by
MAC, and vm targets come from devices(kind=vm) carrying their latched family
+ vm_n.
"""
import re

from .vlan_policy import desired_vid, phase_for


def _internal_to_arista(port: str) -> str:
    """et6b1 -> Ethernet6/1. Mirrors flax_observe.port_worker."""
    m = re.match(r"^et(\d+)b(\d+)$", port)
    if not m:
        return port
    return f"Ethernet{m.group(1)}/{m.group(2)}"


def derive_targets(observe_rows: list[dict], switch_facts: dict,
                   devices: list[dict], *,
                   fp_to_vid: dict, geom_tokens: dict,
                   no_steer: set, bmc_only: set = frozenset(),
                   resolve=None) -> list[dict]:
    """Emit a target per reservable MAC, carrying the GUARDED steered vid.

    bmc/host targets come from observe_state (so they still reserve when
    discover is down), with family LEFT-JOINed from devices (default
    'unknown'). vm targets come from devices(kind=vm), carrying family + vm_n
    (they need discover's stable numbering; best-effort).

    The port's current access_vid is read from switch_facts; the emitted vid is
    desired_vid(family, phase, ...) — current vid for unknown families, the
    family/phase steered vid for known ones (vlan_policy §5b).

    Returns: [{switch, port, mac, kind, vid, family, phase, vm_n?}, ...].
    `phase` is the resolved role for the port (triage/post/...) -- carried so
    the triage cycle can scope its owner=triage reservation write to
    triage-resolved ports while still emitting desired_port steering for all.
    Skips:
      - observe rows where bmc_mac is None (port not enrolled yet)
      - ports with no access_vid (trunks, missing entries)
      - ports in no_steer (excluded uplinks: no reservation, no desired_port)
      - any target whose desired_vid resolves to None

    resolve: optional (switch, port_arista) -> role|None callable from the
    role registry (flax_classify.role_registry.resolve_role, partially
    applied). When given, it REPLACES phase_for as the phase source; None
    (unassigned, only reachable without a catch_all role) skips the target
    the same way a no-steer port does. When resolve is None (default), the
    legacy geometry-based phase_for is used -- byte-identical to pre-registry
    behaviour.
    """
    fam_by_mac = {d["mac"]: (d.get("latched") or {}).get("family", "unknown")
                  for d in devices}

    def port_access(sw, port_internal):
        """-> (access_vid, port_arista) or None to skip the port.

        None when: switch unknown, port not access (no access_vid / trunk), or
        the port is on the no-steer list.
        """
        sw_facts = switch_facts.get(sw)
        if not sw_facts:
            return None
        port_arista = _internal_to_arista(port_internal)
        access_vid = sw_facts.get("ports", {}).get(port_arista, {}).get(
            "access_vid")
        if access_vid is None:             # trunk / missing -> skip
            return None
        if (sw, port_arista) in no_steer:  # excluded uplink -> skip
            return None
        return access_vid, port_arista

    def phase_of(sw, port_arista):
        """Resolved role for the port: registry `resolve` when given, else the
        legacy geometry `phase_for`. None => unassigned (skip the port)."""
        return (resolve(sw, port_arista) if resolve
                else phase_for(geom_tokens, sw, port_arista))

    def steered_vid(access_vid, family, phase):
        return desired_vid(family, phase, access_vid=access_vid,
                           in_no_steer=False, fp_to_vid=fp_to_vid)

    out = []
    for row in observe_rows:
        sw = row["switch"]
        port_internal = row["port"]
        resolved = row.get("resolved") or {}
        bmc_mac = resolved.get("bmc_mac")
        nic_mac = resolved.get("nic_mac")
        if not bmc_mac:
            continue
        port = port_access(sw, port_internal)
        if port is None:
            continue
        access_vid, port_arista = port

        # Resolve the port's role ONCE (bmc + host on a port share it). The
        # phase rides on every emitted target so the triage cycle can scope
        # its owner=triage reservation write to triage-resolved ports while
        # STILL steering post-resolved ports' vids (desired_port). None =>
        # unassigned in the registry -> skip the whole port, like no-steer.
        phase = phase_of(sw, port_arista)
        if phase is None:
            continue

        bmc_family = fam_by_mac.get(bmc_mac, "unknown")
        bmc_vid = steered_vid(access_vid, bmc_family, phase)
        if bmc_vid is not None:
            out.append({"switch": sw, "port": port_internal, "mac": bmc_mac,
                        "kind": "bmc", "vid": bmc_vid, "family": bmc_family,
                        "phase": phase})
        # RJ45-LOM (bmc-only families, e.g. capri): one physical port = ONE MAC
        # serving both BMC and host NIC. flax-observe still synthesizes a
        # phantom nic_mac (bmc_mac - 2) for the single-MAC port, but that NIC
        # never DHCPs -> a host reservation would flap forever (lease never
        # matches reservation). The REAL discovered device is the BMC, so we key
        # the decision on the BMC's family; when it's bmc-only we mint the BMC
        # target only and skip the phantom host.
        if nic_mac and (bmc_family or "").lower() not in bmc_only:
            host_family = fam_by_mac.get(nic_mac, "unknown")
            host_vid = steered_vid(access_vid, host_family, phase)
            if host_vid is not None:
                out.append({"switch": sw, "port": port_internal,
                            "mac": nic_mac, "kind": "host", "vid": host_vid,
                            "family": host_family, "phase": phase})

    # VM targets from devices(kind=vm); steered vid from the port's access_vid.
    for d in devices:
        if d.get("kind") != "vm":
            continue
        port = port_access(d["switch"], d["port"])
        if port is None:
            continue
        access_vid, port_arista = port
        phase = phase_of(d["switch"], port_arista)
        if phase is None:
            continue
        latched = d.get("latched") or {}
        vm_family = latched.get("family", "unknown")
        vm_vid = steered_vid(access_vid, vm_family, phase)
        if vm_vid is None:
            continue
        out.append({"switch": d["switch"], "port": d["port"], "mac": d["mac"],
                    "kind": "vm", "vid": vm_vid, "family": vm_family,
                    "vm_n": latched.get("vm_n"), "phase": phase})
    return out
