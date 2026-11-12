# tests/test_flax_post_fwd_enforce_gate.py
from flax_post.fwd import enforce


def test_detect_mode_never_allows():
    assert enforce.gate_allows("et6b1", "detect", []) is False
    assert enforce.gate_allows("et6b1", "detect", ["et6b1"]) is False


def test_enforce_empty_allowlist_allows_all():
    assert enforce.gate_allows("et6b1", "enforce", []) is True


def test_enforce_allowlist_restricts():
    assert enforce.gate_allows("et6b1", "enforce", ["et6b1", "et7b2"]) is True
    assert enforce.gate_allows("et9b4", "enforce", ["et6b1"]) is False
