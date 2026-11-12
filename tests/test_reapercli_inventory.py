"""Inventory facts read from plain YAML, no ansible and no vault (spec §3, §4)."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.lib.inventory import group_override, image_pins, site_hosts, ssh_target  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)


def test_eindhoven_pair_and_addresses():
    sh = site_hosts(SITES["eindhoven"], ROOT, "orbit")
    assert sh.primary == "bang-gouda"
    assert sh.standby == ("bang-edam",)
    gouda = [b for b in sh.bangs if b.name == "bang-gouda"][0]
    assert gouda.mgmt_ip == "192.168.88.21"
    assert sh.on_site is False


@pytest.mark.parametrize("hostname,expected", [
    ("bang-gouda", True), ("bang-gouda.lab.example.net", True), ("orbit", False),
    ("bang-rhythm", False),
])
def test_on_site_uses_short_hostname(hostname, expected):
    assert site_hosts(SITES["eindhoven"], ROOT, hostname).on_site is expected


def test_dormant_host_marked():
    sh = site_hosts(SITES["braintree"], ROOT, "orbit")
    siesta = [b for b in sh.bangs if b.name == "bang-siesta"][0]
    assert siesta.dormant is True
    assert sh.primary == "bang-fiesta"


def test_ssh_target_by_location():
    sh = site_hosts(SITES["lavergne"], ROOT, "orbit")
    rhythm = [b for b in sh.bangs if b.name == "bang-rhythm"][0]
    assert ssh_target(rhythm, on_site=True) == "flax@192.168.88.21"
    assert ssh_target(rhythm, on_site=False) == "flax@bang-rhythm"     # never the ssh config's User


def test_image_pins():
    pins = image_pins(SITES["eindhoven"], ROOT)
    assert pins["apply_flax_post_image_tag"].startswith("0.9.")


def test_group_override():
    assert group_override(SITES["braintree"], ROOT, "apply_rackpost_hosts") == []
    assert group_override(SITES["eindhoven"], ROOT, "apply_rackpost_hosts") is None
