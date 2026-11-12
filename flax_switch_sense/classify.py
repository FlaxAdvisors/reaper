"""LLDP-corroborated BMC/NIC/junk MAC classification.

Lifted from scripts/switchportrecond.py classify_macs. Same decision tree.
"""
import collections

from .mac import (
    normalize_mac, mac_to_int, int_to_mac, is_paired_nic_of,
    _paired_nic_offsets,
)
from . import macmath as macmath_mod


def _highest_paired_partner(lldp_mac, observed):
    """If `lldp_mac` is the NIC-side of a paired-NIC chassis (BMC = NIC +
    2/4/6/8), return the highest observed MAC that fits. Otherwise None.
    """
    base = mac_to_int(lldp_mac)
    partners = [int_to_mac(base + off) for off in _paired_nic_offsets()]
    matches = [p for p in partners if p in observed]
    return max(matches, key=mac_to_int) if matches else None


ClassifiedMacs = collections.namedtuple(
    "ClassifiedMacs",
    ["bmc", "nics", "junk", "classification_source", "lldp_disagreement"],
)


def classify_macs(macs, lldp_neighbors=None, macmath=None):
    """Identify the legitimate BMC on a switch port, the paired NICs,
    and any junk MACs that don't fit either pattern.

    See the BMC redesign spec section 'Bulk fetch + state latching' and
    scripts/switchportrecond.py:classify_macs for the decision tree.

    When ``macmath`` is a per-vid config dict (e.g. from load_macmath_dir),
    it overrides the legacy ±2 pairing heuristic for recognised schemes.
    ``macmath=None`` (the default) restores the original behavior exactly.
    """
    if lldp_neighbors is None:
        lldp_neighbors = []
    if not macs:
        return ClassifiedMacs(None, [], [], "single_mac", False)

    norm = [normalize_mac(m) for m in macs]

    # ------------------------------------------------------------------
    # macmath branch: dispatch on scheme when a config is supplied.
    # ------------------------------------------------------------------
    if macmath is not None:
        scheme = macmath.get("scheme") if isinstance(macmath, dict) else None

        if scheme == "offset":
            bmc, hosts, junk = macmath_mod.classify_offset(
                norm,
                macmath["bmc_offset"],
                macmath["host_offsets"],
            )
            if bmc is None:
                # Computed BMC not observed -> fall through to legacy.
                return classify_macs(macs, lldp_neighbors, macmath=None)
            return ClassifiedMacs(bmc, hosts, junk, "macmath_offset", False)

        if scheme == "distinct_oui":
            bmc, hosts, junk = macmath_mod.classify_distinct_oui(norm)
            return ClassifiedMacs(bmc, hosts, junk, "macmath_distinct_oui", False)

        # Unknown / missing scheme -> fall through to legacy.
        return classify_macs(macs, lldp_neighbors, macmath=None)
    if len(norm) == 1:
        bmc = norm[0]
        nic = int_to_mac(mac_to_int(bmc) - 2)
        return ClassifiedMacs(bmc, [nic], [], "single_mac", False)

    lldp_macs_on_port = []
    for n in lldp_neighbors:
        try:
            m = normalize_mac(n.get("mac", ""))
        except ValueError:
            continue
        if m in norm:
            lldp_macs_on_port.append(m)

    if len(lldp_macs_on_port) == 1:
        lldp_mac = lldp_macs_on_port[0]
        # Some chassis only LLDP from the host OS (e.g. leopard6b4 sends
        # LLDP via ens1f0np0, the BMC RJ45 is silent). In that case the
        # LLDP-matched MAC is the NIC, and the actual BMC is at +2/+4/+6/
        # +8 in the observed MAC list. Pick the highest such partner.
        # See observe_state for et6b4/et8b1/et8b2/et8b4 (2026-05-24).
        higher_partner = _highest_paired_partner(lldp_mac, norm)
        if higher_partner is not None:
            bmc = higher_partner
            nics = [lldp_mac]
            junk = []
            for m in norm:
                if m in (bmc, lldp_mac):
                    continue
                if is_paired_nic_of(m, bmc):
                    nics.append(m)
                else:
                    junk.append({"mac": m,
                                 "reason": "not_paired_with_lldp_bmc"})
            return ClassifiedMacs(bmc, nics, junk, "lldp_nic_inverted", False)

        bmc = lldp_mac
        nics = []
        junk = []
        for m in norm:
            if m == bmc:
                continue
            if is_paired_nic_of(m, bmc):
                nics.append(m)
            else:
                junk.append({"mac": m, "reason": "not_paired_with_lldp_bmc"})
        return ClassifiedMacs(bmc, nics, junk, "lldp", False)

    if len(lldp_macs_on_port) >= 2:
        bmc = max(norm, key=mac_to_int)
        nics = []
        junk = []
        for m in norm:
            if m == bmc:
                continue
            if m in lldp_macs_on_port:
                junk.append({"mac": m, "reason": "multi_lldp_bmc_observed"})
            elif is_paired_nic_of(m, bmc):
                nics.append(m)
            else:
                junk.append({"mac": m, "reason": "not_paired_with_numeric_bmc"})
        return ClassifiedMacs(bmc, nics, junk, "numeric_fallback", True)

    # No LLDP match -> numeric fallback.
    bmc = max(norm, key=mac_to_int)
    nics = []
    junk = []
    for m in norm:
        if m == bmc:
            continue
        if is_paired_nic_of(m, bmc):
            nics.append(m)
        else:
            junk.append({"mac": m, "reason": "not_paired_with_numeric_bmc"})
    return ClassifiedMacs(bmc, nics, junk, "numeric_fallback", False)
