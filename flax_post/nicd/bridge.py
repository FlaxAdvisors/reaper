"""Mirror the nicd per-port row into post_state.vars.fw_nic (top-level key,
shallow-merged by state.set_state) so the rack viewer renders the mlx steps.
The devices list rides along so the UI can show per-card lines. Twin of
flax_post.biosd.bridge (fw_bios)."""


def fw_nic_slice(row: dict) -> dict:
    return {
        "phase": row.get("phase"),
        "devices": row.get("devices") or [],
        "fault_reason": row.get("fault_reason") or "",
    }


def mirror_row(set_state, port, row) -> None:
    set_state(port, fw_nic=fw_nic_slice(row))
