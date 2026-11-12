# tests/test_flax_control_post_bmcfw_view.py
"""Tests for flax_control.post_bmcfw_view — read-only post BMC/NIC/BIOS
firmware fleet view, merged from the post fw file stores (sole-written by
flax_post/fwd, mounted read-only into flax-control)."""
from flax_control import post_bmcfw_view as v


def test_merge_three_stores_per_port():
    bmc = {"et10b1": {"port": "et10b1", "bmc_ip": "172.25.10.101",
                       "phase": "up_to_date", "current_version": "1.0.9",
                       "target_version": "1.0.9", "updated_at": 100.0}}
    nic = {"et10b1": {"phase": "mlx-checked", "updated_at": 90.0}}
    bios = {"et10b1": {"phase": "up_to_date", "updated_at": 80.0}}
    rows = v.fleet_rows(bmc, nic, bios)
    assert len(rows) == 1
    r = rows[0]
    assert r["port"] == "et10b1" and r["bmc"]["phase"] == "up_to_date"
    assert r["nic"]["phase"] == "mlx-checked" and r["bios"]["phase"] == "up_to_date"


def test_missing_stores_empty():
    assert v.fleet_rows(None, None, None) == []


def test_last_updated_is_max():
    assert v.last_updated({"a": {"updated_at": 5.0}}, {"a": {"updated_at": 9.0}}, None) == 9.0
