# tests/test_apply_switch_snmp_sonic.py
"""roles/apply_switch_snmp SONiC support (tasks/sonic.yml + filter_plugins/
sonic_snmp.py). Offline: fake running config_db, fake community."""
import importlib.util
import json
import os

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

if _ROOT:
    _spec = importlib.util.spec_from_file_location(
        "sonic_snmp", os.path.join(_ROOT, "filter_plugins", "sonic_snmp.py"))
    ss = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ss)

COMM = "fake-ro-comm"
WANT = dict(community=COMM, location="eindhoven", contact="ops@example.com")


def _plan(running):
    return ss.sonic_snmp_plan(json.dumps(running), **WANT)


def test_fresh_switch_gets_all_settings():
    plan = _plan({"FEATURE": {"snmp": {"state": "enabled"}}})
    assert plan["cmds"] == [
        "sudo -n config snmp community add fake-ro-comm RO </dev/null",
        "sudo -n config snmp location add eindhoven </dev/null",
        "sudo -n config snmp contact add ops ops@example.com </dev/null",
    ]
    assert not any(COMM in c for c in plan["changes"])


def test_in_sync_switch_is_no_change():
    plan = _plan({"SNMP_COMMUNITY": {COMM: {"TYPE": "RO"}, "other": {"TYPE": "RO"}},
                  "SNMP": {"LOCATION": {"Location": "eindhoven"},
                           "CONTACT": {"ops": "ops@example.com"}},
                  "FEATURE": {"snmp": {"state": "enabled"}}})
    assert plan == {"cmds": [], "changes": []}


def test_drift_modifies_replaces_and_enables():
    plan = _plan({"SNMP_COMMUNITY": {COMM: {"TYPE": "RW"}},
                  "SNMP": {"LOCATION": {"Location": "old site"},
                           "CONTACT": {"noc": "noc@example.com"}},
                  "FEATURE": {"snmp": {"state": "disabled"}}})
    assert plan["cmds"] == [
        "sudo -n config snmp community del fake-ro-comm </dev/null",
        "sudo -n config snmp community add fake-ro-comm RO </dev/null",
        "sudo -n config snmp location modify eindhoven </dev/null",
        "sudo -n config snmp contact del noc </dev/null",
        "sudo -n config snmp contact add ops ops@example.com </dev/null",
        "sudo -n config feature state snmp enabled </dev/null",
    ]


def test_location_with_space_is_quoted():
    plan = ss.sonic_snmp_plan("{}", COMM, "rack 1", "ops@example.com")
    assert "sudo -n config snmp location add 'rack 1' </dev/null" in plan["cmds"]


def test_sonic_tasks_no_log_and_play_wired():
    tasks = yaml.safe_load(open(os.path.join(
        _ROOT, "roles", "apply_switch_snmp", "tasks", "sonic.yml")))
    by_name = {t["name"]: t for t in tasks}
    assert by_name["Read running config_db"]["no_log"] is True
    write = by_name["Configure SNMP community + sysinfo on SONiC"]
    assert write["no_log"] is True and write["notify"] == "Save SONiC config"
    plays = yaml.safe_load(open(os.path.join(_ROOT, "playbooks", "apply_switch_snmp.yml")))
    sonic = [p for p in plays if p["hosts"] == "sonic"]
    assert sonic[0]["tasks"][0]["ansible.builtin.import_role"]["tasks_from"] == "sonic"
