import json, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from nic_fw_lock import scan

_LOCKED = """Image type:            FS4
FW Version:            14.32.1010
PSID:                  HP_2420110034
Security Attributes:   secure-fw
"""
_OPEN = """Image type:            FS4
FW Version:            14.32.1010
PSID:                  MT_2450111034
Security Attributes:   N/A
"""

def _write(d, dev, text):
    open(os.path.join(d, "mstflint-d_%s_query.txt" % dev), "w").write(text)

def test_locked_card(tmp_path):
    _write(str(tmp_path), "3b:00.0", _LOCKED)
    out = scan(str(tmp_path))
    assert out["locked"] is True
    assert out["cards"][0]["pci"] == "3b:00.0"
    assert out["cards"][0]["psid"] == "HP_2420110034"
    assert out["cards"][0]["security"] == "secure-fw"
    assert out["cards"][0]["locked"] is True

def test_unlocked_card(tmp_path):
    _write(str(tmp_path), "3b:00.0", _OPEN)
    out = scan(str(tmp_path))
    assert out["locked"] is False
    assert out["cards"][0]["locked"] is False

def test_mixed_any_locked(tmp_path):
    _write(str(tmp_path), "3b:00.0", _OPEN)
    _write(str(tmp_path), "5e:00.0", _LOCKED)
    out = scan(str(tmp_path))
    assert out["locked"] is True
    assert len(out["cards"]) == 2

def test_empty_dir(tmp_path):
    out = scan(str(tmp_path))
    assert out == {"locked": False, "cards": []}
