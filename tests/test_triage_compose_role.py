"""apply_triage_compose stages what `reaper deploy <site> triage` fetched from the triage
repo (spec 2026-11-08-reaper-triage-deploy-design): reaper keeps no compose of its own."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles" / "apply_triage_compose"


def _tasks():
    return yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())


def test_role_asserts_src_set():
    first = _tasks()[0]
    a = first["ansible.builtin.assert"]
    assert any("apply_triage_compose_src" in t and "length > 0" in t for t in a["that"])
    assert "reaper deploy <site> triage" in a["fail_msg"]


def test_role_stages_three_files_from_src():
    copies = {t["ansible.builtin.copy"]["dest"]: t["ansible.builtin.copy"] for t in _tasks()
              if "ansible.builtin.copy" in t and "src" in t["ansible.builtin.copy"]}
    want = {"docker-compose-prod.yml": "{{ apply_triage_compose_compose_file }}", ".env": ".env",
            ".env_prod": ".env_prod"}
    for src, dest in want.items():
        c = copies["{{ apply_triage_compose_path }}/" + dest]
        assert c["src"] == "{{ apply_triage_compose_src }}/" + src
        assert c["mode"] == "0644"
    assert len(copies) == 3


def test_no_vendored_compose_left():
    assert not (ROLE / "files").exists()
    assert not (ROOT / "files" / "configs" / "lavergne" / "services" / "triage").exists()


def test_untagged_bang_services_run_skips_triage_instead_of_failing():
    """A fresh-box run of apply_bang_services.yml has no fetched files: it must skip the
    triage stack (deployed later by reaper deploy), not stop every role after it."""
    play = yaml.safe_load((ROOT / "playbooks" / "apply_bang_services.yml").read_text())
    [imp] = [t for p in play for t in p.get("tasks", []) + p.get("roles", [])
             if isinstance(t, dict) and (t.get("ansible.builtin.import_role") or {}).get("name") == "apply_triage_compose"]
    when = imp["when"]
    assert "apply_triage_compose_src" in when and "'triage_compose' in ansible_run_tags" in when
