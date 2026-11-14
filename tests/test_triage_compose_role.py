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


def test_role_creates_every_narrowed_mount_source():
    """The triage compose (TODO-NET-EXPOSURE 9.4) mounts single /etc/flax paths into the
    SOL server and the api. A missing bind source is created by docker as a root-owned
    DIRECTORY, which breaks a file mount, so the role makes each one first."""
    tasks = _tasks()
    dirs = set()
    for t in tasks:
        f = t.get("ansible.builtin.file") or {}
        if f.get("state") == "directory":
            dirs.update(t.get("loop") or [f.get("path")])
    assert {"/etc/flax/soltriage", "/etc/flax/bmcfw-state", "/etc/flax/triage-ui",
            "/etc/flax/mezzflash"} <= dirs
    seeded = {t["ansible.builtin.copy"]["dest"]: t["ansible.builtin.copy"] for t in tasks
              if "ansible.builtin.copy" in t and "content" in t["ansible.builtin.copy"]}
    for f in ("/etc/flax/flash.json", "/etc/flax/bmcfw.json"):
        assert seeded[f]["force"] is False, f


def test_bmc_credentials_change_restarts_the_sol_server():
    """The SOL server mounts credentials-bmc.json as a single file, which pins the inode:
    a rewritten file must restart it (not the whole stack), and only when it changed."""
    tasks = yaml.safe_load((ROOT / "roles" / "apply_lease_agent" / "tasks" / "main.yml").read_text())
    i = next(n for n, t in enumerate(tasks)
             if (t.get("ansible.builtin.copy") or {}).get("dest") == "/etc/flax/credentials-bmc.json")
    reg = tasks[i].get("register")
    assert reg
    restart = [t for t in tasks[i + 1:] if "prod-server-1" in str(t)]
    assert restart and reg + " is changed" in str(restart[0].get("when"))
    assert "restart" in str(restart[0])
