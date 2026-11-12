"""blades.fw_gate_passed: the one Firmware gate (spec 2026-09-11 post-slot-ladder §7)."""
import pytest
from flax_post import blades


@pytest.mark.parametrize("phase", ["up_to_date", "done", "oem", "unsupported"])
def test_terminal_phases_pass_in_any_mode(phase):
    assert blades.fw_gate_passed({"phase": phase, "mode": "detect"}) is True
    assert blades.fw_gate_passed({"phase": phase, "mode": "enforce"}) is True


def test_needs_update_passes_only_in_detect():
    assert blades.fw_gate_passed({"phase": "needs_update", "mode": "detect"}) is True
    assert blades.fw_gate_passed({"phase": "needs_update", "mode": "enforce"}) is False


def test_needs_update_missing_mode_reads_as_detect():
    # rows written before the upgrade carry no mode; the site pins are all detect
    assert blades.fw_gate_passed({"phase": "needs_update"}) is True


def test_explicit_mode_argument_wins_over_row():
    assert blades.fw_gate_passed({"phase": "needs_update", "mode": "detect"}, mode="enforce") is False


@pytest.mark.parametrize("phase", ["checking", "unreachable", "unknown", "fault",
                                   "flashing", "monitoring", "activating", None])
def test_everything_else_waits(phase):
    assert blades.fw_gate_passed({"phase": phase, "mode": "detect"}) is False


def test_missing_slice_waits():
    assert blades.fw_gate_passed(None) is False
    assert blades.fw_gate_passed({}) is False


def test_bios_unsupported_does_not_pass_the_gate():
    """Ruling 2026-10-06: a check that does not match the expected string
    FAILS. A stale biosd `unsupported` row (platform not in the manifest, BIOS
    version never compared) must not open the gate."""
    ok = {"phase": "up_to_date"}
    st = {"fw_bmc": ok, "fw_bios": {"phase": "unsupported"}, "fw_nic": ok}
    assert blades.fw_gates_passed(st) is False
    assert blades.fw_gates_passed(dict(st, fw_bios=ok)) is True      # presence proof
    st["ladder"] = {"fault": {"rung": "fw-gates", "reason": "x"}}
    assert blades.fw_gate_fault_step(st) == "bios-updated"


def test_bios_unsupported_renders_bios_updated_as_fault():
    steps = blades._firmware_steps({"fw_bios": {"phase": "unsupported"}})
    assert steps["bios-checked"] == "done" and steps["bios-updated"] == "fault"
