"""apply_lease_agent ships each site's services/switches.json (2026-11-18: it was
hand-placed, so braintree's rabbit-fiesta entry never reached switch-sense)."""
import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/apply_lease_agent"


def test_switches_json_is_deployed_and_restarts_its_readers():
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    [t] = [t for t in tasks if t.get("name") == "Deploy switches.json"]
    assert t["ansible.builtin.copy"]["src"].endswith("/services/switches.json")
    assert t["ansible.builtin.copy"]["dest"] == "/etc/flax/switches.json"
    handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
    [h] = [h for h in handlers if h["name"] == t["notify"]]
    cmd = h["ansible.builtin.shell"]
    assert "flax-switch-sense" in cmd and "flax-reconcile" in cmd


def test_every_site_has_a_valid_switches_json():
    for site in ("braintree", "eindhoven", "lavergne"):
        entries = json.loads((ROOT / f"files/configs/{site}/services/switches.json").read_text())
        assert entries and all({"name", "driver", "host"} <= set(e) for e in entries), site
