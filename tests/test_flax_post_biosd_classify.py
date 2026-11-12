from flax_post.biosd import classify

def test_classify_version_compare():
    assert classify.classify("TPC_P26C", "TPC_P26C") == "up_to_date"
    assert classify.classify("TPC_P25C", "TPC_P26C") == "needs_update"
    assert classify.classify(None, "TPC_P26C") == "unknown"

def test_flash_eligible_requires_all_gates():
    ok = dict(port="et6b1", mode="enforce", allowlist=[], fw_bmc_phase="up_to_date",
              ssh_ok=True, phase="needs_update")
    assert classify.flash_eligible(**ok) is True
    assert classify.flash_eligible(**{**ok, "mode": "detect"}) is False       # detect never
    assert classify.flash_eligible(**{**ok, "fw_bmc_phase": "needs_update"}) is False  # BMC gate
    assert classify.flash_eligible(**{**ok, "ssh_ok": False}) is False          # host down
    assert classify.flash_eligible(**{**ok, "phase": "up_to_date"}) is False    # nothing to do
    assert classify.flash_eligible(**{**ok, "allowlist": ["et9b9"]}) is False   # not allowlisted
