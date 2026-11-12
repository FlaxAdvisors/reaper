from flax_post.biosd import bridge

def test_fw_bios_slice_projects_ui_fields():
    row = {"phase": "needs_update", "current": "TPC_P25C", "target": "TPC_P26C",
           "fault_reason": "", "internal": "ignored"}
    assert bridge.fw_bios_slice(row) == {"phase": "needs_update", "current": "TPC_P25C",
                                         "target": "TPC_P26C", "fault_reason": "", "mode": None}

def test_mirror_row_writes_fw_bios_slice():
    calls = []
    bridge.mirror_row(lambda port, **kw: calls.append((port, kw)), "et6b1",
                      {"phase": "up_to_date", "current": "TPC_P26C", "target": "TPC_P26C"})
    assert calls == [("et6b1", {"fw_bios": {"phase": "up_to_date", "current": "TPC_P26C",
                                            "target": "TPC_P26C", "fault_reason": "", "mode": None}})]

def test_slice_carries_mode():
    assert bridge.fw_bios_slice({"phase": "needs_update", "mode": "detect"})["mode"] == "detect"
