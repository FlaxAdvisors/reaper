"""bang-v6-addrs must be a started unit, not just a script that ran (bang-blues 2026-11-09:
installed after boot, the deploy ran the script directly, the unit stayed inactive (dead) and
the site health board's ServiceDown fired)."""
import os

import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))

with open(os.path.join(_REAPER, "roles", "apply_bang_services", "tasks", "main.yml")) as f:
    TASKS = yaml.safe_load(f)


def _v6():
    return [t for t in TASKS if "v6_addrs" in (t.get("tags") or [])]


def test_the_unit_is_enabled_and_started():
    t = next(t for t in _v6() if t["name"] == "Enable bang-v6-addrs.service")
    assert t["ansible.builtin.systemd"]["state"] == "started"


def test_a_changed_script_or_unit_restarts_the_unit():
    apply = next(t for t in _v6() if t["name"] == "Apply the v6 addresses now")
    sd = apply["ansible.builtin.systemd"]
    assert sd == {"name": "bang-v6-addrs.service", "state": "restarted"}
    assert apply["when"] == "_v6_addrs_script is changed or _v6_addrs_unit is changed"
