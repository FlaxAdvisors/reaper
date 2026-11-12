import json
from flax_reconcile.config import load_config, DEFAULTS


def test_defaults_match_reaper_values():
    assert DEFAULTS["sweep_interval_secs"] == 900
    assert DEFAULTS["flap_hold_seconds"] == 2
    assert DEFAULTS["kick_cooldown_secs"] == 60
    assert DEFAULTS["bmc_ll_probe_interval_secs"] == 60
    assert DEFAULTS["max_attempts"] == 3
    assert DEFAULTS["sentinel_grace_secs"] == 30
    assert DEFAULTS["debounce_secs"] == 2


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(str(tmp_path / "nope.json"))
    assert cfg == DEFAULTS


def test_partial_file_merges_over_defaults(tmp_path):
    p = tmp_path / "reconcile.json"
    p.write_text(json.dumps({"max_attempts": 5, "unknown_key": 1}))
    cfg = load_config(str(p))
    assert cfg["max_attempts"] == 5          # overridden
    assert cfg["sweep_interval_secs"] == 900  # default kept
    assert "unknown_key" not in cfg           # unknown ignored


def test_malformed_file_is_fatal(tmp_path):
    p = tmp_path / "reconcile.json"
    p.write_text("{ not json")
    try:
        load_config(str(p))
        assert False, "expected ValueError"
    except ValueError:
        pass
