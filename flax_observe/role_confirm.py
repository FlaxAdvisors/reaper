"""Probe-evidence-driven BMC/host role confirmation.

Pure, DB-free decision function. MAC-ordering heuristics (macmath /
distinct_oui / single-MAC) choose *candidates*; the authoritative *role
label* is confirmed here by what each MAC actually answered to:

- A MAC that answers an OpenBMC/IPMI/weutil probe IS a BMC.
- A MAC that answers a common host login IS a host.

A flip away from the heuristic requires a *positive* contradicting signal,
never mere unreachability — transient probe blips keep the heuristic.
"""

from collections import namedtuple

RoleVerdict = namedtuple("RoleVerdict", ["bmc_mac", "nic_mac", "source", "multi_bmc"])

# The probe_bmc_kind results that count as a POSITIVELY-IDENTIFIED BMC (a
# confirmed role, not the heuristic guess): SSH-openbmc, IPMI-traditional, and
# Redfish (a BMC reachable only over its Redfish service, e.g. braintree's
# TiogaPass AMI BMCs -- SSH closed / AMI, no usable IPMI). The single source of
# truth; flax_observe.state_machine imports this so the role-confirmation gates
# there stay in lock-step (an anomalous-primary host-probe / cache-reuse must
# treat every confirmed kind alike). NOT the same set as the inband-admin gate,
# which is SSH/IPMI-only by nature (KCS priv grant) and stays its own literal.
CONFIRMED_BMC_KINDS = ("openbmc", "traditional", "redfish")
_BMC_KINDS = CONFIRMED_BMC_KINDS


def confirm_roles(primary_bmc, heuristic_nics, evidence):
    """Decide authoritative bmc/nic role labels from per-MAC probe evidence.

    Args:
        primary_bmc: the heuristic's chosen BMC MAC (classified.bmc), or None.
        heuristic_nics: classified.nics (may include a phantom nic).
        evidence: {mac: {"bmc_kind": "openbmc"|"traditional"|"unknown"|None,
                          "host_ok": "ok"|"fail"|"unknown"|None}}
            for the *visible* MACs that were probed. Insertion order is
            meaningful (primary first); a MAC absent from evidence was not
            probed (no contradiction -> trust the heuristic for it).

    Returns:
        RoleVerdict(bmc_mac, nic_mac, source, multi_bmc).
    """
    heuristic_nics = list(heuristic_nics or [])

    # Empty evidence -> pure heuristic fallback.
    if not evidence:
        bmc_mac = primary_bmc
        nic_mac = _first_heuristic_nic(heuristic_nics, bmc_mac)
        return RoleVerdict(bmc_mac, nic_mac, "heuristic", [])

    confirmed = [m for m, e in evidence.items()
                 if e.get("bmc_kind") in _BMC_KINDS]

    if len(confirmed) >= 1:
        multi_bmc = confirmed if len(confirmed) > 1 else []
        bmc_mac = primary_bmc if primary_bmc in confirmed else confirmed[0]
        source = "probe_confirmed" if bmc_mac == primary_bmc else "probe_promote_bmc"
    elif primary_bmc and evidence.get(primary_bmc, {}).get("host_ok") == "ok":
        bmc_mac = None
        multi_bmc = []
        source = "probe_flip_host"
    else:
        bmc_mac = primary_bmc
        multi_bmc = []
        source = "heuristic"

    nic_mac = _pick_nic(bmc_mac, heuristic_nics, evidence)
    return RoleVerdict(bmc_mac, nic_mac, source, multi_bmc)


def _pick_nic(bmc_mac, heuristic_nics, evidence):
    # First MAC that positively answered a host login and isn't the BMC.
    for m, e in evidence.items():
        if e.get("host_ok") == "ok" and m != bmc_mac:
            return m
    # Else preserve today's behaviour: first heuristic nic that isn't the BMC.
    return _first_heuristic_nic(heuristic_nics, bmc_mac)


def _first_heuristic_nic(heuristic_nics, bmc_mac):
    for m in heuristic_nics:
        if m != bmc_mac:
            return m
    return None
