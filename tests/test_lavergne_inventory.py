"""Lavergne rebuilt as an eindhoven site (spec 2026-10-27-lavergne-eindhoven-rebuild).

Pins the inventory decisions the spec makes: vids 16-33 on 172.<vid>.0.0/16,
rhythm .1 / blues .2 / VIP .10, 31-33 as EHV-32 pools, Docker ranges clear of
the guest wifi (rhythm's only tailscale path), wlan0-only WAN for this rework.
Reads the YAML unrendered; Jinja strings stay strings.
"""
import ipaddress
import os

import yaml

_REAPER = os.path.join(os.path.dirname(__file__), os.pardir)
_INV = os.path.join(os.path.abspath(_REAPER), "inventories", "lavergne")


def _load(*parts):
    with open(os.path.join(_INV, *parts)) as f:
        return yaml.safe_load(f) or {}


VARS = _load("group_vars", "all", "vars.yml")
JUMPHOST_VARS = _load("group_vars", "jumphosts", "vars.yml")
VLANS = VARS.get("vlans", [])
BY_VID = {v["vid"]: v for v in VLANS}


def test_vlans_cover_16_to_33():
    assert [v["vid"] for v in VARS["vlans"]] == list(range(16, 34))


def test_vlan_parents_follow_lavergne_names():
    parent = {v["vid"]: v["parent"] for v in VARS["vlans"]}
    assert parent[26] == parent[27] == "mgmt"
    assert all(parent[v] == "mezz" for v in set(range(16, 34)) - {26, 27})


def test_vlan_addressing_matches_eindhoven():
    for v in VARS["vlans"]:
        assert v["cidr"] == "172.%d.0.0/16" % v["vid"]
        assert v["gateway_vip"] == "172.%d.0.10" % v["vid"]


def test_pools_31_32_33_are_ehv32_posture():
    for vid in (31, 32, 33):
        v = BY_VID[vid]
        assert (v["family"], v["phase"], v["dhcp_mode"]) == ("tiogapass", "post", "pool")
        assert "pxe" not in v
        assert vid not in VARS["flax_role_post_vid_by_family"].values()


def test_ha_pattern():
    assert VARS["bang_host_suffix"] == {"bang-rhythm": 1, "bang-blues": 2}
    assert VARS["bang_vrrp_priority"] == {"bang-rhythm": 110, "bang-blues": 90}
    assert VARS["apply_keepalived_master_host"] == "bang-rhythm"
    assert VARS["bang_mgmt_vip"] == "192.168.88.10"
    assert {p["ip"] for p in VARS["apply_flax_stack_peers"]} == {"192.168.88.21", "192.168.88.22"}
    assert _load("host_vars", "bang-blues.yml")["apply_flax_stack_is_primary"] is False


def test_docker_ranges_clear_of_site_nets():
    pool = ipaddress.ip_network(VARS["apply_docker_pool_base"])
    bip = ipaddress.ip_interface(VARS["apply_docker_bip"]).network
    nets = ["192.168.88.0/24", "192.168.100.0/23"] + ["172.%d.0.0/16" % v for v in range(16, 34)]
    for n in nets:
        n = ipaddress.ip_network(n)
        assert not pool.overlaps(n), n
        assert not bip.overlaps(n), n


def test_wan_is_wlan0_only_this_rework():
    assert JUMPHOST_VARS["jumphost_wan_failover_wan_failover_enabled"] is False
    assert VARS["bang_wan_iface"] == "corp"
    assert VARS["bang_mgmt_iface"] == "mgmt"


def test_roles_registry():
    assert VARS["flax_role_post_switches"] == ["rabbit-blues"]
    assert VARS["flax_role_post_prefixes"] == ["turtle"]
    assert VARS["flax_role_post_catch_all"] is False
    assert VARS["flax_role_integra_ports"] == {}
    # post_iperf_server is derived for every site (playbooks/group_vars/all/placement.yml):
    # the standby -- at lavergne bang-blues, since bang-rhythm is the keepalived master
    assert "post_iperf_server" not in VARS
    assert VARS["apply_keepalived_master_host"] == "bang-rhythm"


def test_switch_suffixes_and_domain():
    assert VARS["switch_host_suffix"] == {
        "rabbit-rhythm": 3, "rabbit-blues": 4, "turtle-rhythm": 5,
        "rabbit-howlin": 6, "rabbit-c2g": 7, "rabbit-muddy": 8}
    assert VARS["bang_lab_domain"] == "lvq.lab"


def test_switches_proxy_through_rhythm_at_target_mgmt_ips():
    hosts = _load("hosts.yml")["all"]["children"]
    arista = hosts["arista"]["hosts"]
    assert {h: arista[h]["ansible_host"] for h in arista} == {
        "rabbit-rhythm": "192.168.88.31", "rabbit-blues": "192.168.88.32",
        "rabbit-howlin": "192.168.88.34", "rabbit-c2g": "192.168.88.35",
        "rabbit-muddy": "192.168.88.37"}
    assert hosts["cumulus"]["hosts"]["turtle-rhythm"]["ansible_host"] == "192.168.88.33"
    for h in list(arista.values()) + [hosts["cumulus"]["hosts"]["turtle-rhythm"]]:
        assert "bang-rhythm" in h["ansible_ssh_extra_args"]


def test_health_primary_is_rhythm():
    # since the site health board (2026-11-09) the per-site primary is the federation target
    path = os.path.join(os.path.abspath(_REAPER), "roles", "apply_health_compose", "defaults", "main.yml")
    with open(path) as f:
        assert yaml.safe_load(f)["apply_health_compose_federate"]["lavergne"] == "bang-rhythm"


def test_apply_jumphost_refuses_at_lavergne():
    # Review C1: `reaper push lavergne <switch>` stages apply_jumphost on the bangs,
    # which installs every ifcfg and restarts network -> wlan0/tailscale bounce.
    assert JUMPHOST_VARS["apply_jumphost_allowed"] is False
    path = os.path.join(os.path.abspath(_REAPER), "roles", "apply_jumphost", "tasks", "main.yml")
    with open(path) as f:
        first = f.read().split("- name:", 2)[1]
    assert "apply_jumphost_allowed" in first and "assert" in first
