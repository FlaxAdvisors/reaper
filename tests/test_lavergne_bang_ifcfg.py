"""Lavergne bang ifcfg target sets (spec 2026-10-27-lavergne-eindhoven-rebuild §6).

These files are installed ONE AT A TIME with wicked ifup/ifdown (never
apply_jumphost / a network restart: wlan0 is rhythm's only tailscale path).
"""
import os

import pytest

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
CFG = os.path.join(_REAPER, "files", "configs", "lavergne")
LAB = [v for v in range(16, 34) if v not in (26, 27)]
SUFFIX = {"bang-rhythm": 1, "bang-blues": 2}
BANGS = pytest.mark.parametrize("bang", sorted(SUFFIX))


def ifcfg_files(bang):
    d = os.path.join(CFG, bang, "config")
    return {f for f in os.listdir(d) if f.startswith("ifcfg-")}


def raw(bang, name):
    with open(os.path.join(CFG, bang, "config", name)) as f:
        return f.read()


def kv(bang, name):
    out = {}
    for line in raw(bang, name).splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


@BANGS
def test_file_set(bang):
    want = {"ifcfg-mgmt", "ifcfg-corp", "ifcfg-mezz", "ifcfg-wlan0", "ifcfg-unk1", "ifcfg-unk2",
            "ifcfg-unk3", "ifcfg-mgmt.26", "ifcfg-mgmt.27"} | {"ifcfg-mezz.%d" % v for v in LAB}
    assert ifcfg_files(bang) == want


@BANGS
def test_mgmt_addr(bang):
    assert kv(bang, "ifcfg-mgmt")["IPADDR"] == {
        "bang-rhythm": "192.168.88.21/24", "bang-blues": "192.168.88.22/24"}[bang]


@BANGS
def test_lab_addrs(bang):
    s = SUFFIX[bang]
    for v in LAB:
        f = kv(bang, "ifcfg-mezz.%d" % v)
        assert (f["IPADDR"], f["ETHERDEVICE"], f["VLAN_ID"], f["ZONE"]) == (
            "172.%d.0.%d/16" % (v, s), "mezz", str(v), "lab-data"), v
    for v in (26, 27):
        f = kv(bang, "ifcfg-mgmt.%d" % v)
        assert (f["IPADDR"], f["ETHERDEVICE"], f["VLAN_ID"]) == ("172.%d.0.%d/16" % (v, s), "mgmt", str(v))


@BANGS
def test_corp_is_primary_wan(bang):
    # Eindhoven pattern (ifcfg-eth0.2 / ifcfg-wlan0): DHCP route priorities, no
    # failover daemon -- corp drop 100 wins, guest wifi 200 is the fallback.
    f = kv(bang, "ifcfg-corp")
    assert (f["ETHERDEVICE"], f["VLAN_ID"], f["BOOTPROTO"], f["ZONE"]) == ("mgmt", "2", "dhcp", "external")
    assert (f["DHCLIENT_SET_DEFAULT_ROUTE"], f["DHCLIENT_ROUTE_PRIORITY"]) == ("yes", "100")


@BANGS
def test_wlan0_is_backup_wan(bang):
    f = kv(bang, "ifcfg-wlan0")
    assert (f["DHCLIENT_SET_DEFAULT_ROUTE"], f["DHCLIENT_ROUTE_PRIORITY"]) == ("yes", "200")


@BANGS
def test_wlan0_untouched(bang):
    assert kv(bang, "ifcfg-wlan0")["BOOTPROTO"] == "dhcp"
    assert "{{ vault_cfg_wlan_wpa_psk }}" in raw(bang, "ifcfg-wlan0")
