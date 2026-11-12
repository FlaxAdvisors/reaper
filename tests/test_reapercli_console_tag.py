"""apply_flax_stack's flax_console tag (spec §8.4): re-render flax-stack.env WITHOUT the
Restart flax-stack handler (it bounces postgres), then recreate flax-control alone."""
import os
from pathlib import Path

import pytest
import yaml

pytest.importorskip("reapercli")

REAPER = Path(__file__).resolve().parents[1]
TASKS = REAPER / "roles" / "apply_flax_stack" / "tasks"


def console():
    return yaml.safe_load((TASKS / "console.yml").read_text())


def test_console_tasks_never_notify():
    assert [t["name"] for t in console() if "notify" in t] == []


def test_env_render_is_no_log():
    tpl = [t for t in console() if "ansible.builtin.template" in t]
    assert len(tpl) == 1
    assert tpl[0]["no_log"] is True
    assert tpl[0]["ansible.builtin.template"]["dest"] == "{{ apply_flax_stack_env_path }}"


def test_recreate_uses_env_file_and_no_deps():
    cmd = [t for t in console() if "ansible.builtin.command" in t]
    assert len(cmd) == 1
    line = cmd[0]["ansible.builtin.command"]["cmd"]
    for word in ("--env-file", "-p flax-stack", "up -d --no-deps", "flax-control"):
        assert word in line
    assert "postgres" not in line


def test_gated_on_run_tags():
    for t in console():
        when = t["when"] if isinstance(t["when"], str) else " ".join(t["when"])
        assert "ansible_run_tags" in when and "flax_console" in when


def test_imported_with_tag():
    main = yaml.safe_load((TASKS / "main.yml").read_text())
    imp = [t for t in main if t.get("ansible.builtin.import_tasks") == "console.yml"]
    assert len(imp) == 1 and imp[0]["tags"] == ["flax_console"]


def test_secrets_assert_runs_under_console_tag():
    """--tags flax_console renders the env too: a blank vault value must still stop it."""
    main = yaml.safe_load((TASKS / "main.yml").read_text())
    [a] = [t for t in main if t.get("name") == "Assert flax-stack secrets are wired"]
    assert "flax_console" in a.get("tags", [])
