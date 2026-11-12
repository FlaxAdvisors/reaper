"""Plan 2026-11-09-site-health-board Task 1: snmp_exporter's community comes from the
site vault (it was a literal `public`), and the rendered file is private."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/apply_health_compose"


def test_snmp_community_comes_from_the_vault():
    text = (ROLE / "templates/snmp/snmp.yml.j2").read_text()
    assert 'community: "{{ vault_cfg_snmp_ro_community }}"' in text
    assert "public" not in text
    assert not (ROLE / "files/snmp/snmp.yml").exists()


def test_snmp_stage_task_is_private():
    [t] = [t for t in yaml.safe_load((ROLE / "tasks/main.yml").read_text()) if t["name"] == "Stage snmp.yml"]
    assert t["ansible.builtin.template"]["src"] == "snmp/snmp.yml.j2"
    assert t["ansible.builtin.template"]["mode"] == "0600" and t["no_log"] is True


def test_prometheus_asks_for_the_site_auth():
    text = (ROLE / "templates/prometheus/prometheus.yml.j2").read_text()
    job = text[text.index("job_name: snmp_switches"):text.index("relabel_configs", text.index("job_name: snmp_switches"))]
    assert "auth: [site_v2]" in job


def test_the_older_push_role_uses_the_same_vaulted_community():
    # apply_switch_snmp (imperative) and the committed switch configs (apply_config.yml)
    # must never push different communities; and never a 'public' fallback.
    d = (ROOT / "roles/apply_switch_snmp/defaults/main.yml").read_text()
    assert 'apply_switch_snmp_community: "{{ vault_cfg_snmp_ro_community }}"' in d
    assert "default('public')" not in d
