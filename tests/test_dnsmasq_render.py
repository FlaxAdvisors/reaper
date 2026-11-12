# tests/test_dnsmasq_render.py
"""Render tests for the bang_dhcp_server switch in dnsmasq.conf.j2."""
import os
import jinja2
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VLANS = [
    {"vid": 16, "family": "leopard", "phase": "triage", "parent": "eth0", "cidr": "172.16.0.0/16", "gateway_vip": "172.16.0.10"},
    {"vid": 17, "family": "tiogapass", "phase": "triage", "parent": "eth0", "cidr": "172.17.0.0/16", "gateway_vip": "172.17.0.10"},
]
BASE = dict(
    vlans=VLANS, bang_wan_iface="public", bang_mgmt_iface="mgmt",
    bang_mgmt_vip="192.168.88.10", bang_lab_domain="brt.lab",
    apply_bang_services_upstream_dns=["1.1.1.1"],
    apply_bang_services_tftp_root="/srv/tftpboot",
    apply_bang_services_dhcp_pool_start=50, apply_bang_services_dhcp_pool_end=200,
    apply_bang_services_dhcp_lease="12h",
    mgmt_guest_pool={"start": "192.168.88.200", "end": "192.168.88.249", "lease": "8h"},
)


def _render(ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(f"{REPO}/roles/apply_bang_services/templates"),
        trim_blocks=True, lstrip_blocks=False)
    return env.get_template("dnsmasq.conf.j2").render(**ctx)


def test_kea_mode_drops_all_dhcp_keeps_dns_tftp():
    out = _render(dict(BASE, bang_dhcp_server="kea"))
    # DHCP gone
    assert "dhcp-range=" not in out
    assert "dhcp-option=" not in out
    assert "dhcp-boot=" not in out
    assert "dhcp-script=" not in out
    assert "dhcp-hostsdir=" not in out
    assert "enable-ra" not in out
    # DNS + TFTP kept
    assert "enable-tftp" in out
    assert "log-facility=/var/log/dnsmasq/dnsmasq.log" in out
    assert "hostsdir=/etc/dnsmasq.hosts" in out
    assert "domain=brt.lab" in out
    # still listens on the vlan ifaces for DNS
    assert "interface=eth0.17" in out
    assert "interface=mgmt" in out


def test_dnsmasq_mode_default_unchanged():
    """Default (no bang_dhcp_server) and explicit 'dnsmasq' render
    identically, and the DHCP surface is present (regression guard for
    lavergne/eindhoven)."""
    default = _render(BASE)
    explicit = _render(dict(BASE, bang_dhcp_server="dnsmasq"))
    assert default == explicit
    assert "dhcp-range=set:v17" in default
    assert "dhcp-hostsdir=/etc/dnsmasq.dhcp-hosts" in default
    # the per-event hook fed the decommissioned reaper-leased daemon; it was
    # removed (flax-reconcile writes the switches now) and must not come back
    assert "dhcp-script=" not in default
