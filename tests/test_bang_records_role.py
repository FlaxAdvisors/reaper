"""apply_bang_records (FOLLOWUP-20261109 item 8, operator 2026-11-09): journals persist
across reboots (they lived in RAM only, so a reboot erased what diag reads and gouda's
had already rotated past its own boot), capped; rasdaemon keeps decoded machine-check /
memory / PCIe error records, so a CATERR's cause survives the reset. No kdump: pstore
(erst) already keeps a panic's last messages."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/apply_bang_records"


def _tasks():
    return yaml.safe_load((ROLE / "tasks/main.yml").read_text())


def test_the_journal_is_persistent_and_capped():
    [t] = [t for t in _tasks() if "journald.conf.d" in str(t.get("ansible.builtin.copy", {}).get("dest", ""))]
    body = t["ansible.builtin.copy"]["content"]
    assert "[Journal]" in body and "Storage=persistent" in body
    assert "SystemMaxUse={{ apply_bang_records_journal_max_use }}" in body
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text())["apply_bang_records_journal_max_use"] == "2G"
    assert any(t.get("ansible.builtin.file", {}).get("path") == "/var/log/journal" for t in _tasks())


def test_a_config_change_restarts_journald_and_flushes():
    h = str(yaml.safe_load((ROLE / "handlers/main.yml").read_text()))
    assert "restart systemd-journald" in h and "journalctl --flush" in h


def test_rasdaemon_is_installed_and_running():
    pkgs = [t for t in _tasks() if t.get("community.general.zypper", {}).get("name") == "rasdaemon"]
    svc = [t for t in _tasks() if t.get("ansible.builtin.systemd", {}).get("name") == "rasdaemon"]
    assert pkgs and svc and svc[0]["ansible.builtin.systemd"]["enabled"] is True


def test_the_role_ships_on_its_own_component():
    play = (ROOT / "playbooks/apply_bang_services.yml").read_text()
    assert "name: apply_bang_records" in play and "tags: [bang_records]" in play
    c = yaml.safe_load((ROOT / "files/reaper/components.yml").read_text())["bang_records"]
    assert c["tags"] == ["bang_records"] and "rasdaemon" in c["units"] and c["placement"] == "both"
