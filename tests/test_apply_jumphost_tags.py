"""apply_jumphost --tags ssh_keys: refresh flax's authorized_keys without touching the
network (no ifcfg, no netconfig, no reload handler)."""
import os
from pathlib import Path

import yaml

REAPER = Path(__file__).resolve().parents[1]
TASKS = yaml.safe_load((REAPER / "roles" / "apply_jumphost" / "tasks" / "main.yml").read_text())


def tagged(tag):
    return [t for t in TASKS if tag in t.get("tags", [])]


def test_ssh_keys_tag_covers_every_authorized_keys_task():
    keys = [t["name"] for t in TASKS if "authorized_keys" in t["name"]]
    assert keys == ["Deploy authorized_keys"]
    assert [t["name"] for t in tagged("ssh_keys")] == keys


def test_authorized_keys_come_from_the_site_vault_not_a_committed_file():
    """The list lives in the site vault and is written straight to the bang; no committed
    authorized_keys or *.pub in between."""
    t = tagged("ssh_keys")[0]["ansible.builtin.copy"]
    assert t["content"] == "{{ vault_flax_authorized_keys | join('\n') }}\n"   # YAML \n = a real newline
    assert t["dest"] == "~/.ssh/authorized_keys" and "src" not in t
    assert "files/ssh" not in (REAPER / "roles" / "apply_jumphost" / "tasks" / "main.yml").read_text()
    collect = (REAPER / "roles" / "collect_jumphost" / "tasks" / "main.yml").read_text()
    assert "/show/authorized_keys" in collect and "/config/authorized_keys" not in collect


def test_ssh_keys_tag_never_touches_network():
    for t in tagged("ssh_keys"):
        assert "notify" not in t and "become" not in t
        assert "network" not in yaml.safe_dump(t)
