"""filter_plugins/steered_ports.py: apply_config.yml must not push the access VLAN of a
port flax-reconcile steers (geometry / post-geometry / turtle-geometry, minus no-steer).
The committed running-config only snapshots whatever reconcile had set at fetch time.
"""
import importlib.util
import json
import os

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _mod():
    spec = importlib.util.spec_from_file_location(
        "steered_ports", os.path.join(_ROOT, "filter_plugins", "steered_ports.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _services(tmp_path, geometry=(), post=(), turtle=(), no_steer=()):
    d = tmp_path / "services"
    d.mkdir()
    (d / "geometry.json").write_text(json.dumps(list(geometry)))
    (d / "post-geometry.json").write_text(json.dumps({"racks": [], "slots": list(post)}))
    (d / "turtle-geometry.json").write_text(json.dumps(list(turtle)))
    (d / "no-steer-ports.json").write_text(json.dumps(list(no_steer)))
    return str(d)


def test_steered_ports_unions_geometries_for_this_switch_minus_no_steer(tmp_path):
    m = _mod()
    svc = _services(
        tmp_path,
        geometry=[{"switch": "rabbit-a", "port": "et25b1", "ou": "16L"},
                  {"switch": "rabbit-a", "port": "et29b2", "ou": "22L"},
                  {"switch": "rabbit-b", "port": "et5b1", "ou": "1"}],
        post=[{"switch": "rabbit-a", "port": "et9b4", "bottom_ou": 1}],
        turtle=[{"switch": "turtle-a", "port": "swp12", "ou": "T12"}],
        no_steer=[{"switch": "rabbit-a", "port": "Ethernet29/2"}])
    assert m.steered_ports(svc, "rabbit-a") == ["Ethernet25/1", "Ethernet9/4"]
    assert m.steered_ports(svc, "turtle-a") == ["swp12"]
    assert m.steered_ports(svc, "rabbit-zzz") == []


def test_steered_ports_missing_files_mean_none(tmp_path):
    m = _mod()
    d = tmp_path / "services"
    d.mkdir()
    assert m.steered_ports(str(d), "rabbit-a") == []


def test_steered_ports_malformed_file_raises(tmp_path):
    m = _mod()
    svc = _services(tmp_path)
    with open(os.path.join(svc, "geometry.json"), "w") as f:
        f.write("{not json")
    with pytest.raises(Exception):
        m.steered_ports(svc, "rabbit-a")


EOS = """hostname rabbit-a
!
interface Ethernet25/1
   description mezz-flash station (OU16)
   speed forced 25gfull
   switchport access vlan 16
!
interface Ethernet25/10
   switchport access vlan 4
!
interface Ethernet28/1
   switchport access vlan 33
!
interface Ethernet29/1
   shutdown
   switchport access vlan 17
!
interface Management1
   ip address 192.168.88.31/24
!
"""


def test_eos_drops_access_vlan_only_on_steered_ports():
    m = _mod()
    out = m.eos_without_steered_vlans(EOS, ["Ethernet25/1", "Ethernet29/1"])
    assert out == EOS.replace("   speed forced 25gfull\n   switchport access vlan 16\n",
                              "   speed forced 25gfull\n").replace(
        "   shutdown\n   switchport access vlan 17\n", "   shutdown\n")
    # a port whose name merely starts with a steered one is untouched
    assert "interface Ethernet25/10\n   switchport access vlan 4\n" in out
    assert "interface Ethernet28/1\n   switchport access vlan 33\n" in out


def test_eos_no_steered_ports_is_identity():
    m = _mod()
    assert m.eos_without_steered_vlans(EOS, []) == EOS


DESIRED = """auto swp11
iface swp11
    bridge-access 4

auto swp12
iface swp12
    bridge-access 16

auto swp13
iface swp13
    bridge-access 16

auto swp49
iface swp49
    bridge-vids 2 4
"""
LIVE = DESIRED.replace("iface swp12\n    bridge-access 16", "iface swp12\n    bridge-access 17") \
              .replace("iface swp11\n    bridge-access 4", "iface swp11\n    bridge-access 5") \
              .replace("\n", "\r\n")


def test_cumulus_keeps_live_access_vlan_on_steered_ports():
    m = _mod()
    out = m.cumulus_keep_steered_vlans(DESIRED, LIVE, ["swp12", "swp13"])
    assert "iface swp12\n    bridge-access 17\n" in out      # live kept
    assert "iface swp13\n    bridge-access 16\n" in out      # equal anyway
    assert "iface swp11\n    bridge-access 4\n" in out       # not steered: file wins
    assert "\r" not in out
    assert out.count("bridge-access") == 3


def test_cumulus_port_missing_live_keeps_desired():
    m = _mod()
    live = LIVE.replace("auto swp12\r\niface swp12\r\n    bridge-access 17\r\n\r\n", "")
    out = m.cumulus_keep_steered_vlans(DESIRED, live, ["swp12"])
    assert "iface swp12\n    bridge-access 16\n" in out


def _play(name):
    with open(os.path.join(_ROOT, "playbooks", "apply_config.yml")) as f:
        plays = yaml.safe_load(f)
    return next(p for p in plays if p["name"] == name)


def test_apply_config_wires_the_filters():
    arista = _play("Apply Arista config")
    eos = next(t for t in arista["tasks"] if "arista.eos.eos_config" in t)
    assert eos["arista.eos.eos_config"]["src"] == "{{ push_src }}"
    assert arista["vars"]["push_src"].endswith(".push-config.txt")
    text = open(os.path.join(_ROOT, "playbooks", "apply_config.yml")).read()
    assert "eos_without_steered_vlans" in text
    assert "cumulus_keep_steered_vlans" in text
    first = _play("Apply Cisco IOS config")["tasks"][0]
    assert "ansible.builtin.assert" in first
    # SONiC: a steered port's VLAN membership is left to reconcile, the rest pushes
    # (operator 2026-11-18: refuse only the managed ports' VLAN assignment)
    sonic = _play("Apply SONiC config")
    assert not any("ansible.builtin.assert" in t and "steered" in str(t) for t in sonic["tasks"])
    render = next(t for t in sonic["tasks"] if t.get("name") == "Render config_db.json")
    assert "sonic_without_steered_vlans(steered)" in render["ansible.builtin.set_fact"]["sonic_rendered"]


def test_crlf_to_lf():
    m = _mod()
    assert m.crlf_to_lf("a\r\nb\r\n\r\nc") == "a\nb\n\nc"
    assert m.crlf_to_lf("a\nb") == "a\nb"


def test_cumulus_compares_crlf_normalised_live_content():
    # A Jinja replace('\r', '') never matched, so every turtle push was "changed"
    # (rewrite + ifreload -a) and every plan reported a change.
    cum = _play("Apply Cumulus config")
    fact = next(t for t in cum["tasks"] if "cumulus_live" in t.get("ansible.builtin.set_fact", {}))
    assert fact["ansible.builtin.set_fact"]["cumulus_live"] == "{{ live_interfaces.stdout | crlf_to_lf }}"
    text = open(os.path.join(_ROOT, "playbooks", "apply_config.yml")).read()
    assert "replace('\\r'" not in text


# ---- SONiC: config_db VLAN membership of steered ports stays reconcile's ----
SONIC_DB = {
    "PORT": {"Ethernet20": {"alias": "Ethernet6/1", "speed": "25000"},
             "Ethernet21": {"alias": "Ethernet6/2", "speed": "25000"},
             "Ethernet124": {"alias": "Ethernet32/1", "speed": "100000"}},
    "VLAN": {"Vlan24": {"vlanid": "24"}, "Vlan1000": {"vlanid": "1000"}},
    "VLAN_MEMBER": {"Vlan1000|Ethernet20": {"tagging_mode": "untagged"},
                    "Vlan24|Ethernet21": {"tagging_mode": "untagged"},
                    "Vlan24|Ethernet124": {"tagging_mode": "tagged"}},
}


def test_sonic_drops_vlan_members_of_steered_ports_by_alias():
    m = _mod()
    out = json.loads(m.sonic_without_steered_vlans(json.dumps(SONIC_DB), ["Ethernet6/2"]))
    assert "Vlan24|Ethernet21" not in out["VLAN_MEMBER"]
    assert set(out["VLAN_MEMBER"]) == {"Vlan1000|Ethernet20", "Vlan24|Ethernet124"}
    assert out["PORT"] == SONIC_DB["PORT"] and out["VLAN"] == SONIC_DB["VLAN"]


def test_sonic_matches_a_native_port_name_too():
    m = _mod()
    out = json.loads(m.sonic_without_steered_vlans(json.dumps(SONIC_DB), ["Ethernet20"]))
    assert "Vlan1000|Ethernet20" not in out["VLAN_MEMBER"]
    assert "Vlan24|Ethernet21" in out["VLAN_MEMBER"]


def test_sonic_no_steered_ports_keeps_every_member():
    m = _mod()
    out = json.loads(m.sonic_without_steered_vlans(json.dumps(SONIC_DB), []))
    assert out == SONIC_DB
