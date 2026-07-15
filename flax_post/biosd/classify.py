"""BIOS version classification + the composite flash gate.
Reuses the BMC agent's mode/allowlist gate; adds the BIOS-specific
prerequisites (BMC known-good + host reachable)."""
from flax_post.fwd.enforce import gate_allows


def classify(current, target: str) -> str:
    if not current:
        return "unknown"
    return "up_to_date" if current == target else "needs_update"


def flash_eligible(port, mode, allowlist, fw_bmc_phase, ssh_ok, phase) -> bool:
    return (gate_allows(port, mode, allowlist)
            and fw_bmc_phase == "up_to_date"
            and bool(ssh_ok)
            and phase == "needs_update")
