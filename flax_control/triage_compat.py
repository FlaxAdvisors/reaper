"""Switchportrecond-shaped status.json builders for Triage UI back-compat.

Source-of-truth contract: scripts/switchportrecond.py:render_status_snapshot
(the function the React UI's IServerStatusResponse type matches against).
"""
import re
from typing import Any


# State variable order (same as switchportrecond's STATE_VARS).
# Used to compute 'deepest reached' for the state field.
_VARS_ORDER = (
    "linkstate", "bmcmac", "bmcip", "bmcping", "bmcipmi", "bmcpower",
    "chassissn", "nodeip", "nodeping", "nodepxe", "nodessh", "inventory",
)

# Per-var success set. A var is "reached" iff its value is in this set —
# matches scripts/switchportrecond.py:_deepest_state exactly so the Triage
# UI's state label is byte-identical. Note: bmcpower:'off' and
# inventory:'absent' do NOT count as reached (they're real transitions but
# not "success" for the UI's progress-bar semantics).
_SUCCESS = {
    "linkstate": {"link"},
    "bmcmac":    {"found"}, "bmcip":     {"found"}, "bmcping":   {"ok"},
    # 'redfish' is a positively-identified BMC kind too (observe's Redfish
    # probe path -- braintree TiogaPass AMI BMCs); it must count as bmcipmi
    # "reached" so the Triage tile advances past bmcping, same as openbmc/
    # traditional. Mirrors flax_observe.role_confirm.CONFIRMED_BMC_KINDS.
    "bmcipmi":   {"openbmc", "traditional", "redfish"},
    "bmcpower":  {"on"},    "chassissn": {"found"},
    "nodeip":    {"found"}, "nodeping":  {"ok"},    "nodepxe":   {"found"},
    "nodessh":   {"ok"},    "inventory": {"found"},
}


def display_port(port: str) -> str:
    """et6b1 → Et6/1 (passthrough for non-Arista names)."""
    m = re.match(r"^et(\d+)b(\d+)$", port)
    if not m:
        return port
    return f"Et{m.group(1)}/{m.group(2)}"


def internal_port(port: str) -> str:
    """Et6/1 OR Ethernet6/1 → et6b1 (passthrough for non-Arista names).

    Must accept BOTH the short (Et6/1) and long (Ethernet6/1) Arista forms:
    the switch-overview page links use the long form (the switch_facts ports
    key), so observe_state / reconcile-status lookups (keyed on the internal
    et6b1 form) would otherwise miss entirely.
    """
    m = re.match(r"^Et(?:hernet)?(\d+)/(\d+)$", port)
    if not m:
        return port.lower().replace(" ", "")
    return f"et{m.group(1)}b{m.group(2)}"


def arista_port(port: str) -> str:
    """et6b1 → Ethernet6/1 (inverse of internal_port's et-direction).

    The reconcile flow is Arista-canonical (spec §6): the flap and the
    intentional_flap sentinel use Ethernet6/1. devices.port (the device page's
    flap form value) is internal short form (et6b1), so the operator POST path
    must canonicalize before enqueueing. Idempotent on already-canonical Arista
    names and a passthrough for non-Arista names (swp6), so it is always safe to
    apply -- the switch_detail page already passes the Arista URL form.
    """
    m = re.match(r"^et(\d+)b(\d+)$", port)
    if not m:
        return port
    return f"Ethernet{m.group(1)}/{m.group(2)}"


def _deepest_state_var(vars_: dict[str, Any]) -> str:
    """Return the name of the deepest STATE_VAR reached.

    'Reached' means value is in the per-var _SUCCESS set — matches
    scripts/switchportrecond.py:_deepest_state. Walk STATE_VARS in order;
    the last var whose value is in its success set wins.

    When NOTHING has progressed we must disambiguate two physically distinct
    cases that both leave every var at its non-success value:

      * linkstate=link, nothing deeper  -> the port has LINK (a DUT is cabled
        and the switch sees carrier) but observe hasn't probed further yet.
        Return 'linkstate' so the Triage tile colours pale-blue ("has link").
      * linkstate=nolink/unknown        -> the port has NO link at all. Return
        'off' so the tile renders blank (the missing/off background), NOT the
        pale-blue has-link colour.

    Collapsing both into 'linkstate' (the old behaviour) made a linked port
    indistinguishable from a dark port in the API, so the React tile painted
    BOTH with the blank 'server-missing-background'. The switch reported link
    on the /switch view while the rack tile stayed white. See
    Server.tsx:backgroundClass — 'linkstate' must mean *has link*.
    """
    deepest = None
    for name in _VARS_ORDER:
        slot = vars_.get(name) or {}
        if slot.get("value", "unknown") in _SUCCESS.get(name, set()):
            deepest = name
    if deepest is None:
        # Nothing reached at all (not even linkstate=link): the port is dark.
        # Return 'off' so the React tile paints blank, NOT the pale-blue
        # has-link colour. (A no-link port must not look like a linked one.)
        return "off"
    # `deepest` is the last var whose value was in its success set. If that is
    # only 'linkstate', the port has LINK but observe hasn't probed deeper —
    # the tile should be pale-blue ("has link").
    return deepest


def observe_row_to_triage_status(row: dict[str, Any], *, ou: str) -> dict[str, Any]:
    """Build a switchportrecond-shaped status.json record from one
    observe_state row.

    `ou` is the geometry OU like '20L' (digits + L|C|R). Caller supplies it
    because the observe_state table doesn't carry OU/column itself.
    """
    vars_ = row.get("vars") or {}
    state = _deepest_state_var(vars_)
    state_slot = vars_.get(state) or {}

    ou_match = re.match(r"^(\d+)([LCR])$", ou or "")
    ou_num = ou_match.group(1) if ou_match else (ou or "")
    column = ou_match.group(2) if ou_match else ""

    # Scalar resolved values live in observe_state.resolved (populated by
    # flax-observe's PortWorker). vars[name].value is the state-machine
    # flag ("found"/"unknown") — useful for `state`/`time` but wrong for
    # the data fields the Triage UI renders. Fall back to "unknown" when
    # the resolver hasn't filled the field yet.
    resolved = row.get("resolved") or {}
    return {
        "serviceindex": "0",
        "port":         display_port(row["port"]),
        "ou":           ou_num,
        "column":       column,
        "state":        state,
        "time":         state_slot.get("since") or row.get("last_polled") or "",
        "chassis":      resolved.get("chassis_sn") or "unknown",
        "mac":          resolved.get("bmc_mac")    or "unknown",
        "ip":           resolved.get("bmc_ip")     or "unknown",
        "nodemac":      resolved.get("nic_mac")    or "unknown",
        "nodeip":       resolved.get("nic_ip")     or "unknown",
        # power = the real HSC watts ("11 W", non-zero even when off) or "?"
        # when unreadable. bmcpower = the on/off/unknown status, independent of
        # the wattage and of the composite `state` (which can sit past bmcpower
        # for both on and off hosts). The UI renders the button from BOTH:
        # colour from bmcpower, the watts label from power.
        "power":        resolved.get("bmc_power")  or "?",
        "bmcpower":     (vars_.get("bmcpower") or {}).get("value") or "unknown",
        # Platform fields for the BMC-FW updater (additive; legacy UI ignores them).
        "product_name":       resolved.get("product_name") or "unknown",
        "bmc_kind":           resolved.get("bmc_kind") or "unknown",
        # redfish_version: BMC Redfish service version (unauth service root,
        # host-power-independent). bmc-fw keys on the low OEM AMI version to
        # recognise an un-updatable board. "" when absent (non-redfish BMCs).
        "redfish_version":    resolved.get("redfish_version") or "",
        "link_session_since": resolved.get("link_session_since") or "",
    }
