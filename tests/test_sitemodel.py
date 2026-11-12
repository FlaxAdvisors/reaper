"""Plan 2026-11-09-site-health-board Task 3: the per-site device/link file
(files/configs/<site>/devices.yml) and its loader/validator. Our infrastructure only."""
from pathlib import Path

import pytest
import yaml

from reapercli.lib.sitemodel import Device, End, Link, SiteModel, load_model, validate

ROOT = Path(__file__).resolve().parents[1]

HOSTS = {"all": {"children": {
    "jumphosts": {"hosts": {"bang-a": {}, "bang-b": {}}},
    "arista": {"hosts": {"rabbit-a": {"ansible_host": "192.168.88.31"}}},
    "cisco": {"hosts": {"turtle-a": {"ansible_host": "192.168.88.33"}}}}}}
GOOD = {"site": "s1",
        "devices": [{"name": "bang-a", "kind": "bang", "mgmt_ip": "192.168.88.21", "bmc_ip": "192.168.88.11"},
                    {"name": "bang-b", "kind": "bang", "mgmt_ip": "192.168.88.22", "state": "dormant"},
                    {"name": "turtle-a", "kind": "switch", "vendor": "cisco", "mgmt_ip": "192.168.88.33"},
                    {"name": "rabbit-a", "kind": "switch", "vendor": "arista", "mgmt_ip": "192.168.88.31"},
                    {"name": "corp", "kind": "uplink", "note": "corp switch"}],
        "links": [{"a": "turtle-a:Te1/1/1", "b": "bang-a:eth0", "speed": "10G"},
                  {"a": "rabbit-a:Et2/1", "b": "turtle-a:Te1/1/2", "speed": "10G", "expect": "down",
                   "note": "STP-blocked"}]}


def _root(tmp_path, devices=GOOD, dormant=("bang-b",)):
    (tmp_path / "inventories/s1").mkdir(parents=True)
    (tmp_path / "inventories/s1/hosts.yml").write_text(yaml.safe_dump(HOSTS))
    (tmp_path / "files/reaper").mkdir(parents=True)
    (tmp_path / "files/reaper/sites.yml").write_text(yaml.safe_dump(
        {"sites": {"s1": {"inventory": "inventories/s1", "state": "active", "dormant_hosts": list(dormant)}}}))
    (tmp_path / "files/configs/s1").mkdir(parents=True)
    (tmp_path / "files/configs/s1/devices.yml").write_text(yaml.safe_dump(devices))
    return tmp_path


def test_loads_ends_and_defaults(tmp_path):
    m = load_model(_root(tmp_path), "s1")
    assert isinstance(m, SiteModel) and m.site == "s1"
    d = {x.name: x for x in m.devices}
    assert d["turtle-a"] == Device("turtle-a", "switch", "192.168.88.33", vendor="cisco")
    assert d["bang-a"].state == "live" and d["bang-a"].bmc_ip == "192.168.88.11"
    assert m.links[0] == Link(End("turtle-a", "Te1/1/1"), End("bang-a", "eth0"), "10G")
    assert m.links[1].expect == "down" and m.links[1].note == "STP-blocked"
    assert validate(m, tmp_path) == []


CASES = {
    "unknown link end": lambda d: d["links"].append({"a": "nope:x", "b": "bang-a:eth1", "speed": "1G"}),
    "port used twice": lambda d: d["links"].append({"a": "turtle-a:Te1/1/1", "b": "rabbit-a:Et1/1", "speed": "1G"}),
    "vendor differs": lambda d: d["devices"][2].update(vendor="arista"),
    "inventory switch missing": lambda d: d["devices"].pop(3),
    "dormant not marked": lambda d: d["devices"][1].update(state="live"),
    "unknown state": lambda d: d["devices"][0].update(state="maybe"),
    "unknown kind": lambda d: d["devices"][0].update(kind="server"),
    "not in inventory": lambda d: d["devices"].append({"name": "rabbit-x", "kind": "switch", "vendor": "arista",
                                                       "mgmt_ip": "192.168.88.40"}),
    "switch without mgmt_ip": lambda d: d["devices"][3].pop("mgmt_ip"),
}


@pytest.mark.parametrize("name", list(CASES))
def test_validate_catches(tmp_path, name):
    import copy
    d = copy.deepcopy(GOOD)
    CASES[name](d)
    errs = validate(load_model(_root(tmp_path, d), "s1"), tmp_path)
    assert errs, name


@pytest.mark.parametrize("site", ["eindhoven", "lavergne", "braintree"])
def test_the_three_site_files_validate(site):
    assert validate(load_model(ROOT, site), ROOT) == []
