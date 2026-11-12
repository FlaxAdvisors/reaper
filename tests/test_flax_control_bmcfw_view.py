# tests/test_flax_control_bmcfw_view.py
"""Tests for flax_control.bmcfw_view — read-only BMC-FW fleet view.

All tests use tmp_path + monkeypatch FLAX_CONFIG_DIR; no /etc/flax access.
"""
import json
import pytest


def _write(tmp_path, name, obj):
    (tmp_path / name).write_text(json.dumps(obj))


MANIFEST = {
    "tiogapass-openbmc": {
        "match": {"product_name": ["(?i)tioga", "(?i)wiwynn.*tp"],
                  "bmc_kind": "openbmc"},
        "target_version": "flax-onetree-1.0.6-202606261452",
        "auto": True,
    }
}


def _observe(port="Ethernet10/3", switch="rabbit-gouda", product="Wiwynn Tioga Pass"):
    return {f"{switch}:{port}": {
        "switch": switch, "port": port, "vars": {}, "last_polled": "x",
        "generation": 1,
        "resolved": {"product_name": product, "bmc_kind": "traditional",
                     "bmc_ip": "172.17.10.103", "chassis_sn": "WTH-A"},
    }}


def test_matcher_matches_tioga_returns_target(tmp_path, monkeypatch):
    from flax_control import bmcfw_view
    monkeypatch.setattr(bmcfw_view, "FLAX_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "bmc-firmware-versions.json", MANIFEST)
    m = bmcfw_view.load_matcher()
    assert m.match("Wiwynn Tioga Pass") == (
        "tiogapass-openbmc", "flax-onetree-1.0.6-202606261452")
    assert m.match("Dell PowerEdge") is None
    assert m.match("") is None
    assert m.targets[0]["target_version"] == "flax-onetree-1.0.6-202606261452"


def test_matcher_missing_manifest_matches_nothing(tmp_path, monkeypatch):
    from flax_control import bmcfw_view
    monkeypatch.setattr(bmcfw_view, "FLAX_CONFIG_DIR", str(tmp_path))
    m = bmcfw_view.load_matcher()
    assert m.match("Wiwynn Tioga Pass") is None
    assert m.targets == []


def test_read_store_missing_is_empty(tmp_path, monkeypatch):
    from flax_control import bmcfw_view
    monkeypatch.setattr(bmcfw_view, "FLAX_CONFIG_DIR", str(tmp_path))
    assert bmcfw_view.read_store() == {}


def test_fleet_rows_evaluated_port_uses_store_state(tmp_path, monkeypatch):
    from flax_control import bmcfw_view
    monkeypatch.setattr(bmcfw_view, "FLAX_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "bmc-firmware-versions.json", MANIFEST)
    store = {"et10b3": {"port": "et10b3", "serial": "WTH-A",
                        "bmc_ip": "172.17.10.103",
                        "current_version": "flax-onetree-1.0.6-202606221107",
                        "target_version": "flax-onetree-1.0.6-202606261452",
                        "phase": "needs_update", "percent": 0,
                        "fault_reason": "", "updated_at": 1.0}}
    rows = bmcfw_view.fleet_rows(_observe(), store, bmcfw_view.load_matcher())
    assert len(rows) == 1
    r = rows[0]
    assert r["port"] == "Et10/3"
    assert r["state"] == "needs_update"
    assert r["state_pill"] == "warn"
    assert r["current_version"] == "flax-onetree-1.0.6-202606221107"
    assert r["target_version"] == "flax-onetree-1.0.6-202606261452"
    assert r["evaluated"] is True


def test_fleet_rows_unevaluated_tp_port_marked_not_evaluated(tmp_path, monkeypatch):
    from flax_control import bmcfw_view
    monkeypatch.setattr(bmcfw_view, "FLAX_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "bmc-firmware-versions.json", MANIFEST)
    rows = bmcfw_view.fleet_rows(_observe(), {}, bmcfw_view.load_matcher())
    assert len(rows) == 1
    r = rows[0]
    assert r["state"] == "not evaluated"
    assert r["state_pill"] == "neutral"
    assert r["current_version"] == "—"
    assert r["target_version"] == "flax-onetree-1.0.6-202606261452"  # from manifest
    assert r["bmc_ip"] == "172.17.10.103"  # from resolved
    assert "serial" not in r
    assert r["evaluated"] is False


def test_fleet_rows_excludes_non_tp_ports(tmp_path, monkeypatch):
    from flax_control import bmcfw_view
    monkeypatch.setattr(bmcfw_view, "FLAX_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "bmc-firmware-versions.json", MANIFEST)
    obs = _observe(product="Supermicro X11")
    rows = bmcfw_view.fleet_rows(obs, {}, bmcfw_view.load_matcher())
    assert rows == []
