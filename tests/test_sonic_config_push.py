# tests/test_sonic_config_push.py
"""SONiC declarative push (playbooks/apply_config.yml, filter_plugins/sonic_config.py).

Offline only: fake config_db fixtures and fake vault values. Customer / sales-rack
switches are never in an inventory or files/configs (operator ruling 2026-10-31),
so nothing here talks to a switch.
"""
import importlib.util
import json
import os
import sys

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if _ROOT:
    sc = _load("sonic_config", "filter_plugins/sonic_config.py")
    rs = _load("redact_secrets", "filter_plugins/redact_secrets.py")

# Fake values, nothing real.
VAULT = {
    "vault_cfg_snmp_ro_community": 'fake"ro\\comm',     # needs JSON escaping
    "vault_cfg_tacplus_passkey": "fake-tac-key",
}

RUNNING = {
    "DEVICE_METADATA": {"localhost": {"hostname": "fake-sw", "hwsku": "fake"}},
    "SNMP_COMMUNITY": {'fake"ro\\comm': {"TYPE": "RO"}},
    "TACPLUS": {"global": {"passkey": "fake-tac-key", "timeout": "5"}},
    "PORT": {
        "Ethernet0": {"admin_status": "up", "description": "uplink {{ not jinja }}"},
        "Ethernet1": {"admin_status": "up"},
    },
    "VLAN_MEMBER": {"Vlan10|Ethernet1": {"tagging_mode": "untagged"}},
    "bgpraw": "router bgp 65000\n description {% raw %} braces {{ x }}\n",
}


def _committed(running=RUNNING):
    """What a `reaper fetch` would commit: the running config, redacted."""
    return rs.redact_secrets(json.dumps(running, indent=4))


# ── render ────────────────────────────────────────────────────────────────────

def test_committed_fixture_has_no_secret_and_a_key_placeholder():
    text = _committed()
    assert "fake-tac-key" not in text and "ro\\\\comm" not in text
    assert "{{ vault_cfg_snmp_ro_community }}" in json.loads(text)["SNMP_COMMUNITY"]


def test_placeholders_found_in_keys_and_values():
    assert sc.sonic_placeholders(_committed()) == [
        "vault_cfg_snmp_ro_community", "vault_cfg_tacplus_passkey"]


def test_render_round_trips_to_the_running_config():
    rendered = sc.sonic_render(_committed(), VAULT)
    assert json.loads(rendered) == RUNNING


def test_render_keeps_literal_braces_and_key_order():
    rendered = json.loads(sc.sonic_render(_committed(), VAULT))
    assert rendered["PORT"]["Ethernet0"]["description"] == "uplink {{ not jinja }}"
    assert rendered["bgpraw"] == RUNNING["bgpraw"]
    assert list(rendered) == list(RUNNING)


def test_render_missing_vault_value_raises():
    with pytest.raises(ValueError, match="vault_cfg_tacplus_passkey"):
        sc.sonic_render(_committed(), {"vault_cfg_snmp_ro_community": "x"})


def test_render_unrecognised_placeholder_raises():
    doc = json.dumps({"TACPLUS": {"global": {"passkey": "{{ vault_cfg_x | default('') }}"}}})
    with pytest.raises(ValueError, match="unrecognised"):
        sc.sonic_render(doc, {"vault_cfg_x": "y"})


def test_render_placeholder_inside_text_value():
    doc = json.dumps({"bgpraw": " neighbor 10.0.0.1 password {{ vault_cfg_bgp }}\n"})
    out = json.loads(sc.sonic_render(doc, {"vault_cfg_bgp": "fake-pw"}))
    assert out["bgpraw"] == " neighbor 10.0.0.1 password fake-pw\n"


def test_render_two_keys_to_one_name_raises():
    doc = json.dumps({"SNMP_COMMUNITY": {"{{ vault_cfg_a }}": {}, "{{ vault_cfg_b }}": {}}})
    with pytest.raises(ValueError, match="same"):
        sc.sonic_render(doc, {"vault_cfg_a": "same", "vault_cfg_b": "same"})


# ── diff ──────────────────────────────────────────────────────────────────────

def _rendered():
    return sc.sonic_render(_committed(), VAULT)


def test_diff_clean_when_running_equals_rendered():
    d = sc.sonic_config_diff(json.dumps(RUNNING) + "\r\n", _rendered(), VAULT)
    assert d == {"changed": False, "diff": "", "set": [], "unset": [], "changed_ports": []}


def test_diff_shape_set_unset_ports():
    running = json.loads(json.dumps(RUNNING))
    running["PORT"]["Ethernet1"]["admin_status"] = "down"     # load will fix
    running["PORT"]["Ethernet7"] = {"admin_status": "up"}    # load won't remove
    d = sc.sonic_config_diff(json.dumps(running), _rendered(), VAULT)
    assert d["changed"] is True
    assert d["set"] == ["PORT|Ethernet1 admin_status"]
    assert d["unset"] == ["PORT|Ethernet7 admin_status"]
    assert d["changed_ports"] == ["Ethernet1", "Ethernet7"]
    assert d["diff"].startswith("--- running\n+++ rendered\n")
    assert '-            "admin_status": "down"' in d["diff"]
    assert '-        "Ethernet7": {' in d["diff"]


def test_diff_masks_secrets_and_still_shows_secret_drift():
    running = json.loads(json.dumps(RUNNING))
    running["TACPLUS"]["global"]["passkey"] = "fake-old-key"
    d = sc.sonic_config_diff(json.dumps(running), _rendered(), VAULT)
    out = json.dumps(d)
    for secret in ("fake-old-key", "fake-tac-key", "ro\\\\comm"):
        assert secret not in out
    assert d["set"] == ["TACPLUS|global passkey"]
    assert "<vault_cfg_tacplus_passkey sha256:" in d["diff"]
    assert '"{{ vault_cfg_tacplus_passkey }}"' in d["diff"]


def test_diff_community_drift_shows_as_key_change():
    running = json.loads(json.dumps(RUNNING))
    running["SNMP_COMMUNITY"] = {"fake-other": {"TYPE": "RO"}}
    d = sc.sonic_config_diff(json.dumps(running), _rendered(), VAULT)
    assert "fake-other" not in json.dumps(d)
    assert d["set"] == ["SNMP_COMMUNITY|{{ vault_cfg_snmp_ro_community }} TYPE"]
    assert len(d["unset"]) == 1 and "sha256:" in d["unset"][0]


def test_diff_unparsable_running_raises():
    with pytest.raises(ValueError, match="running"):
        sc.sonic_config_diff("No such container: bgp\n{", _rendered(), VAULT)


# ── critical_ports reads the sonic check result ───────────────────────────────

def test_critical_ports_flags_a_changed_sonic_port(tmp_path):
    sys.path.insert(0, os.path.join(_ROOT, "library"))
    import critical_ports
    (tmp_path / "fake-sw").mkdir()
    (tmp_path / "fake-sw" / ".check_result.json").write_text(json.dumps(
        {"device_type": "sonic", "changed": True, "changed_ports": ["Ethernet1"]}))
    topo = tmp_path / "topology.yml"
    topo.write_text(yaml.safe_dump({"critical_links": [
        {"local_device": "fake-sw", "local_port": "Ethernet1",
         "peer_device": "bang-x", "peer_port": "eth0", "peer_role": "jumphost"}]}))
    crit = critical_ports.analyse_site(str(tmp_path), str(topo))
    assert [(c["device"], c["port"]) for c in crit] == [("fake-sw", "Ethernet1")]


# ── apply_config.yml: a real push needs the operator ─────────────────────────

def _sonic_tasks():
    with open(os.path.join(_ROOT, "playbooks", "apply_config.yml")) as f:
        plays = yaml.safe_load(f)
    play = next(p for p in plays if p.get("hosts") == "sonic")

    def flat(tasks):
        for t in tasks:
            if "block" in t:
                yield t
                yield from flat(t["block"])
                yield from flat(t.get("always", []))
            else:
                yield t
    return list(flat(play["tasks"]))


def test_real_push_needs_operator_confirmation():
    push = next(t for t in _sonic_tasks() if t.get("name") == "Push config_db")
    assert "not ansible_check_mode" in push["when"]
    assert any("sonic_apply_confirm" in w and "not" not in w for w in push["when"])
