"""Firmware step states, copied from Post (flax_post/blades.py _FW_BMC/_FW_BIOS/
_nic_steps and nicd.classify.aggregate) so rackpost's firmware rungs render the same
bmc-checked/bmc-updated, bios-checked/bios-updated, mlx-checked/mlx-updated pairs.

A copy, not an import: flax_rungs imports nothing from flax_post (blades.py needs
psycopg). tests/test_flax_rungs_fwsteps.py asserts equality with the originals."""

FW_STEPS = ("bmc-checked", "bmc-updated", "bios-checked", "bios-updated",
            "mlx-checked", "mlx-updated")

# fwd phase -> (bmc-checked state, bmc-updated state)
FW_BMC = {
    "up_to_date": ("done", "done"), "done": ("done", "done"),
    "needs_update": ("done", "cur"),
    "checking": ("cur", "pending"),
    "flashing": ("done", "cur"), "monitoring": ("done", "cur"),
    "activating": ("done", "cur"),
    "fault": ("done", "fault"),
    "unreachable": ("cur", "pending"),   # attention; re-classifies when the BMC returns
    "oem": ("done", "done"),             # reachable Redfish OEM board, nothing to flash (terminal)
    "unsupported": ("done", "done"),     # nothing to flash here (Facebook-OpenBMC IOM / wedge)
}

# biosd phase -> (bios-checked state, bios-updated state)
FW_BIOS = {
    "up_to_date": ("done", "done"), "done": ("done", "done"),
    "needs_update": ("done", "cur"),
    "checking": ("cur", "pending"),
    "flashing": ("done", "cur"), "activating": ("done", "cur"),
    "fault": ("done", "fault"),
    "unreachable": ("cur", "pending"), "unknown": ("cur", "pending"),
    # platform not in the manifest, BIOS version never compared: an unmatched check fails
    "unsupported": ("done", "fault"),
}


def nic_aggregate(devices: list[dict]) -> tuple[str, str, str]:
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


def nic_steps(st: dict) -> tuple[str, str]:
    """(mlx-checked, mlx-updated) from a vars dict holding `fw_nic` (Post's
    post_state.vars shape). No slice yet -> pending/pending."""
    nic = st.get("fw_nic")
    if not nic:
        return "pending", "pending"
    if nic.get("phase") == "unreachable":
        return "cur", "pending"
    if nic.get("phase") == "fault":
        return "done", "fault"
    checked, updated, _roll = nic_aggregate(nic.get("devices") or [])
    return checked, updated


def bmc_steps(phase: str | None) -> tuple[str, str]:
    """(bmc-checked, bmc-updated) for an fwd phase; unknown/absent -> pending/pending."""
    return FW_BMC.get(phase, ("pending", "pending"))


def bios_steps(phase: str | None) -> tuple[str, str]:
    """(bios-checked, bios-updated) for a biosd phase; unknown/absent -> pending/pending."""
    return FW_BIOS.get(phase, ("pending", "pending"))
