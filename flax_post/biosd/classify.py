"""BIOS version classification + the composite flash gate.
Reuses the BMC agent's mode/allowlist gate; adds the BIOS-specific
prerequisites (BMC known-good + host reachable).

Version semantics live in `version.py` (parity with triage biosfw).
"""
from flax_post.fwd.enforce import gate_allows

from . import version


def classify(current, target) -> str:
    """'unknown' | 'up_to_date' | 'needs_update'.

    Unknown on EITHER side is 'unknown', never 'needs_update': an unreadable
    version (None, blank, or the BMC's literal "null") would otherwise arm a
    flash on every pass, and an absent target would arm a flash toward nothing.
    """
    if version.is_unknown(current) or version.is_unknown(target):
        return "unknown"
    return "up_to_date" if version.same(current, target) else "needs_update"


def flash_eligible(port, mode, allowlist, fw_bmc_phase, ssh_ok, phase) -> bool:
    return (gate_allows(port, mode, allowlist)
            and fw_bmc_phase == "up_to_date"
            and bool(ssh_ok)
            and phase == "needs_update")
