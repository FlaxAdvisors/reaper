from flax_post.fwd import bridge


def test_slice_up_to_date_is_ver_ok():
    row = {"current_version": "1.0.8", "target_version": "1.0.8",
           "phase": "up_to_date", "percent": 0, "fault_reason": ""}
    s = bridge.fw_bmc_slice(row)
    assert s == {"current": "1.0.8", "target": "1.0.8", "ver_class": "ver-ok",
                 "phase": "up_to_date", "mode": None, "percent": 0, "fault_reason": ""}


def test_slice_differing_version_is_ver_warn():
    s = bridge.fw_bmc_slice({"current_version": "1.0.6", "target_version": "1.0.8",
                             "phase": "needs_update", "percent": 0})
    assert s["ver_class"] == "ver-warn"
    assert s["phase"] == "needs_update"
    assert s["fault_reason"] == ""   # defaulted from a row with no fault_reason
    assert s["percent"] == 0         # round-trips from the row


def test_slice_has_no_higher_than_target_class():
    """Triage parity: version.compare is same|differs with no ordering, so a
    current that merely differs from the target renders as a plain mismatch.
    There is no longer a distinct "current is newer" (ver-hi) class."""
    s = bridge.fw_bmc_slice({"current_version": "1.0.9", "target_version": "1.0.8",
                             "phase": "up_to_date"})
    assert s["ver_class"] == "ver-warn"


def test_slice_unknown_version_is_ver_na():
    s = bridge.fw_bmc_slice({"current_version": None, "target_version": "1.0.8",
                             "phase": "unreachable"})
    assert s["ver_class"] == "ver-na"
    assert s["current"] is None


def test_mirror_row_calls_set_state_with_fw_bmc():
    calls = []
    bridge.mirror_row(lambda port, **kw: calls.append((port, kw)), "et6b1",
                      {"current_version": "1.0.8", "target_version": "1.0.8", "phase": "done"})
    assert calls[0][0] == "et6b1"
    assert calls[0][1]["fw_bmc"]["phase"] == "done"
    assert calls[0][1]["fw_bmc"]["ver_class"] == "ver-ok"


def test_fw_bmc_slice_carries_mode():
    from flax_post.fwd import bridge
    assert bridge.fw_bmc_slice({"phase": "needs_update", "mode": "enforce"})["mode"] == "enforce"
    assert bridge.fw_bmc_slice({"phase": "needs_update"})["mode"] is None
