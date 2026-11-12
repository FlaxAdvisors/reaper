# tests/test_apply_radvd_render.py
"""Render tests for apply_radvd radvd.conf (jinja2)."""
import os
import jinja2
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VLANS = [
    {"vid": 16, "parent": "eth0"},
    {"vid": 31, "parent": "eth0"},
]
CTX = dict(vlans=VLANS, bang_mgmt_iface="mgmt",
           apply_radvd_mgmt_v6_prefix="fd00:88",
           apply_radvd_v6_lab_vlans=True)


def _render(ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(f"{REPO}/roles/apply_radvd/templates"),
        trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    return env.get_template("radvd.conf.j2").render(**ctx)


def test_radvd_one_stanza_per_lab_iface_plus_mgmt():
    out = _render(CTX)
    assert "interface eth0.16 {" in out
    assert "interface eth0.31 {" in out
    assert "interface mgmt {" in out
    assert "prefix fd00:16::/64" in out
    assert "prefix fd00:88::/64" in out


def test_radvd_managed_flag_and_zero_lifetime():
    out = _render(CTX)
    assert "AdvManagedFlag on;" in out
    assert "AdvDefaultLifetime 0;" in out
    assert "AdvAutonomous off;" in out   # stateful, no SLAAC


def test_radvd_mgmt_only_when_lab_disabled():
    out = _render(dict(CTX, apply_radvd_v6_lab_vlans=False))
    assert "interface mgmt {" in out
    assert "interface eth0.16 {" not in out
