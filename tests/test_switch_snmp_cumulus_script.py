"""apply_switch_snmp's Cumulus script must leave exactly ONE agent address. Cumulus 3.3.1
(turtle-rhythm) ships `agentAddress  udp:127.0.0.1:161` (camel case); the script only
deleted lowercase `agentaddress`, so both binds stayed and snmpd exited 1 (2026-11-09).
Runs the role's real shell body against a temp snmpd.conf."""
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _script(conf: Path) -> str:
    [t] = yaml.safe_load((ROOT / "roles/apply_switch_snmp/tasks/cumulus.yml").read_text())[:1]
    body = t["ansible.builtin.raw"]
    body = re.sub(r"\{\{\s*apply_switch_snmp_community\s*\}\}", "testcomm", body)
    body = re.sub(r"\{\{\s*apply_switch_snmp_location\s*\}\}", "lavergne", body)
    body = re.sub(r"\{\{\s*apply_switch_snmp_contact\s*\}\}", "ops@example.com", body)
    return body.replace("f=/etc/snmp/snmpd.conf", f"f={conf}")


def test_a_camel_case_loopback_agent_address_is_replaced(tmp_path):
    conf = tmp_path / "snmpd.conf"
    conf.write_text("# cumulus 3.3.1\nagentAddress  udp:127.0.0.1:161\nsysLocation    Sitting on the Dock of the Bay\n")
    out = subprocess.run(["sh", "-c", _script(conf)], capture_output=True, text=True, check=True).stdout
    addrs = [l for l in conf.read_text().splitlines() if re.match(r"\s*agent ?address", l, re.I)]
    assert addrs == ["agentaddress udp:0.0.0.0:161"] and "CHANGED" in out
    out2 = subprocess.run(["sh", "-c", _script(conf)], capture_output=True, text=True, check=True).stdout
    assert out2.strip() == "UNCHANGED"
