"""Per-card BIOS-style verdict + multi-card aggregation + the composite flash
gate. Up-to-date requires FW version AND PSID AND the UEFI boot-ROM bit; any
unmapped PSID turns the mlx-updated step red. Reuses the BMC agent's
mode/allowlist gate and adds NIC prerequisites (BMC + BIOS up_to_date + ssh)."""
from flax_post.fwd.enforce import gate_allows


def classify_device(dev: dict, entry: dict | None) -> dict:
    """Return dev enriched with target/target_psid/phase (+ fault_reason)."""
    out = dict(dev)
    out.setdefault("fault_reason", "")
    if entry is None:
        out.update(target=None, target_psid=None, phase="unsupported")
        return out
    out["target"] = entry["target_version"]
    out["target_psid"] = entry["target_psid"]
    # up_to_date REQUIRES UEFI enabled: the expansion ROM must be on after the
    # FW update or the card won't work in UEFI BIOS boot mode. uefi tri-state:
    # True=enabled (ok), False=present-but-off (enable it), None=knob not exposed
    # (pre-upgrade FW, or target FW that lacks it -> not done, needs attention).
    at_target = (dev.get("current") == entry["target_version"]
                 and dev.get("psid") == entry["target_psid"]
                 and dev.get("uefi") is True)
    if at_target:
        out["phase"] = "up_to_date"
    elif dev.get("secure"):
        out["phase"] = "fault"
        out["fault_reason"] = "fw-locked (secure-fw); cannot flash"
    else:
        out["phase"] = "needs_update"
    return out


def aggregate(devices: list[dict]) -> tuple[str, str, str]:
    """(mlx-checked state, mlx-updated state, roll-up phase). No cards -> done/done."""
    if not devices:
        return ("done", "done", "up_to_date")
    phases = [d.get("phase") for d in devices]
    checked = "done" if all(d.get("current") for d in devices) else "cur"
    if any(p in ("unsupported", "fault") for p in phases):
        updated = "fault"
        roll = "unsupported" if "unsupported" in phases else "fault"
    elif all(p == "up_to_date" for p in phases):
        updated, roll = "done", "up_to_date"
    elif any(p in ("needs_update", "flashing") for p in phases):
        updated = "cur"
        roll = "flashing" if "flashing" in phases else "needs_update"
    else:
        updated, roll = "pending", "checking"
    return (checked, updated, roll)


def flash_eligible(port, mode, allowlist, fw_bmc_phase, fw_bios_phase, ssh_ok, devices) -> bool:
    return (gate_allows(port, mode, allowlist)
            and fw_bmc_phase == "up_to_date"
            and fw_bios_phase == "up_to_date"
            and bool(ssh_ok)
            and any(d.get("phase") == "needs_update" for d in devices))
