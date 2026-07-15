"""Mirror the agent's per-port row into post_state.vars.fw_bios (top-level key,
shallow-merged by state.set_state), so the rack viewer renders the BIOS steps.
Twin of flax_post.fwd.bridge (fw_bmc)."""
_UI_FIELDS = ("phase", "current", "target", "fault_reason")


def fw_bios_slice(row: dict) -> dict:
    return {k: (row.get(k) or "" if k == "fault_reason" else row.get(k)) for k in _UI_FIELDS}


def mirror_row(set_state, port, row) -> None:
    set_state(port, fw_bios=fw_bios_slice(row))
