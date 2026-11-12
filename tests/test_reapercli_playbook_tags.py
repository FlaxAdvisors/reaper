"""Every role import in apply_bang_services.yml is addressable by a tag
(reaper deploy picks components by tag; spec 2026-11-07 §11 Phase 0)."""
import os
import pathlib

import yaml

REAPER = pathlib.Path(__file__).resolve().parents[1]


def _imports():
    play = yaml.safe_load((REAPER / "playbooks" / "apply_bang_services.yml").read_text())[0]
    return [t for t in play["tasks"] if "ansible.builtin.import_role" in t]


def test_every_import_role_is_tagged():
    untagged = [t["ansible.builtin.import_role"]["name"] for t in _imports() if not t.get("tags")]
    assert untagged == []


def test_reconcile_has_new_and_alias_tag():
    rec = [t for t in _imports() if t["ansible.builtin.import_role"]["name"] == "apply_flax_reconcile"]
    assert rec[0]["tags"] == ["flax_reconcile", "apply_flax_reconcile"]


def test_tag_names_unique_per_role():
    firsts = [t["tags"][0] for t in _imports() if t.get("tags")]
    assert len(firsts) == len(set(firsts))
