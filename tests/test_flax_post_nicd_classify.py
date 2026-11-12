# tests/test_flax_post_nicd_classify.py
from flax_post.nicd import classify

_ENTRY = {"dir": "D", "bin": "b", "target_version": "14.32.1010", "target_psid": "MT_2450112034"}

def dev(current="14.32.1010", psid="MT_2450112034", uefi=True, secure=False, pci="5e:00.0"):
    return {"pci": pci, "current": current, "psid": psid, "uefi": uefi, "secure": secure}

def test_classify_up_to_date_needs_version_psid_and_uefi():
    assert classify.classify_device(dev(), _ENTRY)["phase"] == "up_to_date"
    assert classify.classify_device(dev(current="14.20.1202"), _ENTRY)["phase"] == "needs_update"
    assert classify.classify_device(dev(psid="FB_2450111034"), _ENTRY)["phase"] == "needs_update"  # not re-branded yet
    assert classify.classify_device(dev(uefi=False), _ENTRY)["phase"] == "needs_update"

def test_classify_unsupported_when_no_entry():
    assert classify.classify_device(dev(psid="X"), None)["phase"] == "unsupported"

def test_classify_secure_card_not_at_target_is_fault():
    d = classify.classify_device(dev(current="14.20.1202", secure=True), _ENTRY)
    assert d["phase"] == "fault" and "lock" in d["fault_reason"].lower()
    # a secure card already at target is fine
    assert classify.classify_device(dev(secure=True), _ENTRY)["phase"] == "up_to_date"

def test_classify_enriches_targets():
    d = classify.classify_device(dev(current="14.20.1202"), _ENTRY)
    assert d["target"] == "14.32.1010" and d["target_psid"] == "MT_2450112034"

def _cl(*devs):
    return [classify.classify_device(d, _ENTRY if d["psid"] in ("MT_2450112034", "FB_2450111034") else None) for d in devs]

def test_aggregate_all_up_to_date_green():
    assert classify.aggregate(_cl(dev(), dev(pci="d8:00.0"))) == ("done", "done", "up_to_date")

def test_aggregate_any_needs_update_amber():
    chk, upd, ph = classify.aggregate(_cl(dev(), dev(current="14.20.1202", pci="d8:00.0")))
    assert (chk, upd, ph) == ("done", "cur", "needs_update")

def test_aggregate_any_unsupported_red():
    chk, upd, ph = classify.aggregate(_cl(dev(), dev(psid="UNMAPPED", pci="d8:00.0")))
    assert chk == "done" and upd == "fault" and ph == "unsupported"

def test_aggregate_no_cards_is_done_done():
    assert classify.aggregate([]) == ("done", "done", "up_to_date")

def test_flash_eligible_all_conditions():
    devs = _cl(dev(current="14.20.1202"))
    ok = dict(port="et10b1", mode="enforce", allowlist=["et10b1"],
              fw_bmc_phase="up_to_date", fw_bios_phase="up_to_date", ssh_ok=True, devices=devs)
    assert classify.flash_eligible(**ok) is True
    assert classify.flash_eligible(**{**ok, "mode": "detect"}) is False
    assert classify.flash_eligible(**{**ok, "allowlist": ["other"]}) is False
    assert classify.flash_eligible(**{**ok, "fw_bmc_phase": "needs_update"}) is False
    assert classify.flash_eligible(**{**ok, "fw_bios_phase": "checking"}) is False
    assert classify.flash_eligible(**{**ok, "ssh_ok": False}) is False
    assert classify.flash_eligible(**{**ok, "devices": _cl(dev())}) is False  # nothing needs update


def test_classify_uefi_required_for_up_to_date():
    # UEFI must be enabled after the FW update (else no UEFI BIOS boot). So an
    # absent knob (None, pre-upgrade FW) or present-but-off (False) is NOT
    # up_to_date even at target FW+PSID; only uefi True is done.
    assert classify.classify_device(dev(uefi=None), _ENTRY)["phase"] == "needs_update"
    assert classify.classify_device(dev(uefi=False), _ENTRY)["phase"] == "needs_update"
    assert classify.classify_device(dev(uefi=True), _ENTRY)["phase"] == "up_to_date"
