"""radvd is VIP-exclusive like kea: the role disables autostart but never stops it, and its
restart handler only restarts a radvd that is already running. `--tags radvd` stopped radvd on
both lavergne/braintree masters on 2026-11-09 (state: stopped, nothing started it again)."""
import os

import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _y(*p):
    with open(os.path.join(_REAPER, "roles", "apply_radvd", *p)) as f:
        return yaml.safe_load(f)


def test_the_role_never_stops_radvd():
    for t in _y("tasks", "main.yml"):
        sd = t.get("ansible.builtin.systemd") or {}
        if sd.get("name") == "radvd.service":
            assert sd.get("enabled") is False
            assert "state" not in sd


def test_restart_only_touches_a_running_radvd():
    h = next(h for h in _y("handlers", "main.yml") if h["name"] == "Restart radvd")
    assert h["ansible.builtin.shell"] == \
        "systemctl is-active --quiet radvd.service && systemctl restart radvd.service || true"
