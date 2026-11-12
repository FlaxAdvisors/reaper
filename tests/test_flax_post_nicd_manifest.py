import json
from flax_post.nicd import manifest

_M = {
    "FB_2450111034": {"dir": "MCX4411A-ACQN", "bin": "fw-x", "target_version": "14.32.1010", "target_psid": "MT_2450112034"},
    "MT_2450112034": {"dir": "MCX4411A-ACQN", "bin": "fw-x", "target_version": "14.32.1010", "target_psid": "MT_2450112034"},
}

def test_match_by_reported_psid():
    m = manifest.NicMatcher(_M)
    assert m.match("FB_2450111034")["target_psid"] == "MT_2450112034"
    assert m.match("MT_2450112034")["dir"] == "MCX4411A-ACQN"
    assert m.match("UNMAPPED_PSID") is None
    assert m.match("") is None

def test_load_nic_manifest(tmp_path):
    (tmp_path / "nic-firmware-versions.json").write_text(json.dumps(_M))
    assert manifest.load_nic_manifest(str(tmp_path)) == _M

def test_load_missing_or_malformed_is_empty(tmp_path):
    assert manifest.load_nic_manifest(str(tmp_path)) == {}          # no file
    (tmp_path / "nic-firmware-versions.json").write_text("[1,2]")   # wrong type (list)
    assert manifest.load_nic_manifest(str(tmp_path)) == {}
