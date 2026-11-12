"""Grafana admin password from the site vault (operator 2026-11-09): the same value as the
switches (vault_arista_password) unless the site vault names its own; reset only when the
live login fails, and never on a command line or in a log."""
import os

import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _yaml(*p):
    with open(os.path.join(_REAPER, *p)) as f:
        return yaml.safe_load(f)


TASKS = _yaml("roles", "apply_health_compose", "tasks", "main.yml")


def _task(name):
    return next(t for t in TASKS if t.get("name") == name)


def test_password_comes_from_the_vault():
    d = _yaml("roles", "apply_health_compose", "defaults", "main.yml")
    assert d["apply_health_compose_grafana_admin_password"] == \
        "{{ vault_grafana_admin_password | default(vault_arista_password) }}"


def test_probe_then_reset_only_on_401():
    names = [t.get("name") for t in TASKS]
    probe, reset = _task("Probe the grafana admin login"), _task("Set the grafana admin password from the vault")
    assert names.index("Bring health stack up") < names.index(probe["name"]) < names.index(reset["name"])
    assert probe["no_log"] is True and reset["no_log"] is True
    uri = probe["ansible.builtin.uri"]
    assert uri["url_password"] == "{{ apply_health_compose_grafana_admin_password }}"
    assert uri["status_code"] == [200, 401]
    assert reset["when"] == "apply_health_compose_grafana_login.status == 401"


def test_reset_reads_the_password_from_stdin():
    cmd = _task("Set the grafana admin password from the vault")["ansible.builtin.command"]
    assert "--password-from-stdin" in cmd["cmd"]
    assert "apply_health_compose_grafana_admin_password" not in cmd["cmd"]
    assert cmd["stdin"] == "{{ apply_health_compose_grafana_admin_password }}"
