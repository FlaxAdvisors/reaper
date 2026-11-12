"""Plan 2026-11-09-site-health-board Task 2: every live switch's committed config carries a
read-only SNMP community from the site vault (never a literal), so snmp_exporter can walk it.
Offline switches (rabbit-c2g) and switches with no committed config are skipped."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PH = "{{ vault_cfg_snmp_ro_community }}"
OFFLINE = {"rabbit-c2g"}
WANT = {
    "arista": ("running-config.txt", [f"snmp-server community {PH} ro"]),
    "cisco": ("running-config.txt", [f"snmp-server community {PH} RO"]),
    "cumulus": ("show-config-commands.txt", [f"net add snmp-server readonly-community {PH} access any",
                                             "net add snmp-server listening-address udp:0.0.0.0:161"]),
}


def _switches():
    for site in ("eindhoven", "lavergne", "braintree"):
        inv = yaml.safe_load((ROOT / f"inventories/{site}/hosts.yml").read_text())

        def walk(node, group=None):
            for k, v in (node or {}).items():
                if k == "hosts":
                    for h in (v or {}):
                        yield group, h
                elif k == "children":
                    for g, sub in (v or {}).items():
                        yield from walk(sub, g)
        for group, host in walk(inv.get("all", inv)):
            if group in WANT and host not in OFFLINE:
                yield site, group, host


CASES = [c for c in _switches() if (ROOT / f"files/configs/{c[0]}/{c[2]}/config").is_dir()]


def test_the_scan_finds_switches():
    assert {h for _, _, h in CASES} >= {"rabbit-gouda", "turtle-gouda", "turtle-lorax", "rabbit-howlin"}


@pytest.mark.parametrize("site,vendor,host", CASES, ids=[c[2] for c in CASES])
def test_every_live_switch_config_has_a_vaulted_ro_community(site, vendor, host):
    fname, lines = WANT[vendor]
    text = (ROOT / f"files/configs/{site}/{host}/config/{fname}").read_text()
    for line in lines:
        assert line in text.splitlines(), f"{host}: missing {line!r}"
    others = [l for l in text.splitlines()
              if re.search(r"snmp-server (readonly-)?community\s+(?!\{\{)", l)]
    assert others == [], f"{host}: literal community {others}"
