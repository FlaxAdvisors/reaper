"""Plan 2026-11-09-site-health-board Task 9: the physical diagram is generated from
devices.yml (one source with the health board), in render.py's sentinel-colour style."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from reapercli.lib.sitemodel import load_model

ROOT = Path(__file__).resolve().parents[1]


def _dot(site):
    return subprocess.run([sys.executable, str(ROOT / "scripts/site_diagram.py"), "--site", site],
                          capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("site", ["eindhoven", "lavergne", "braintree"])
def test_every_device_and_link_appears_once(site):
    m, text = load_model(ROOT, site), _dot(site)
    for d in m.devices:
        assert len(re.findall(rf'^\s*"{re.escape(d.name)}" \[', text, re.M)) == 1, d.name
    edges = re.findall(r"^\s*\"[^\"]+\":\w+(?::\w)? -- \"[^\"]+\":\w+(?::\w)? \[", text, re.M)
    assert len(edges) == len(m.links)


def test_not_live_devices_and_expect_down_links_are_dashed():
    text = _dot("lavergne")
    node = re.search(r'^\s*"rabbit-c2g" \[[^\n]*', text, re.M).group(0)
    assert "dashed" in node
    eh = _dot("eindhoven")
    [edge] = [l for l in eh.splitlines() if '"rabbit-gouda":' in l and '"rabbit-edam":' in l and "Et2_1" in l]
    assert "dashed" in edge and "#0a0a06" in edge


def test_speed_colours_and_no_customer_or_dut_nodes():
    eh = _dot("eindhoven")
    assert "#0a0a04" in eh and "#0a0a03" in eh and "#0a0a02" in eh and "#0a0a05" in eh
    for word in ("F4", "RRORD", "Zone E", "sled", "W400"):
        assert word not in eh


@pytest.mark.skipif(subprocess.run(["which", "dot"], capture_output=True).returncode, reason="no graphviz")
def test_graphviz_parses_it():
    subprocess.run(["dot", "-Tsvg", "-o", "/dev/null"], input=_dot("lavergne"), text=True, check=True)
