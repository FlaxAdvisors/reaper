import pathlib, subprocess, pytest
pytest.importorskip("flax_rungs")
from flax_rungs.portname import LldpEntry, wedge_iface
from flax_rungs.ha import check
from flax_rack.geometry import load_geometry, parse_geometry

F = pathlib.Path(__file__).parent / "fixtures/rackpost"
MACS = {"L37": {"ens3f0np0": "88:e9:a4:00:00:01", "ens3f1np1": "88:e9:a4:00:00:02"},
        "L05": {"ens3f0np0": "88:e9:a4:00:00:21", "ens3f1np1": "88:e9:a4:00:00:22"}}


def E(iface, dev, mac, descr="ens3f0np0"):
    return LldpEntry(iface, dev, mac, descr)


def geo(links37=None):
    import json
    d = json.load(open(F / "rr101-geometry.json"))
    d["devices"] = [x for x in d["devices"] if x["pos"] in ("L37", "L05")]
    if links37:
        d["devices"][0]["links"].update(links37)
    return parse_geometry(d)


def good():
    return {"A": [E("Ethernet31", "rr101-l37", "88:e9:a4:00:00:01"),
                  E("Ethernet108", "localhost", "88:e9:a4:00:00:21")],
            "B": [E("Ethernet31", "rr101-l37", "88:e9:a4:00:00:02", "ens3f1np1"),
                  E("Ethernet108", "rr101-l05", "88:e9:a4:00:00:22", "ens3f1np1")]}


def test_wedge_iface():
    assert wedge_iface(28, 1) == "Ethernet108" and wedge_iface(8, 4) == "Ethernet31"
    assert wedge_iface(8, None) == "Ethernet28"


def test_correct_cabling_passes_even_with_localhost():
    r = check(geo(), good(), MACS)
    assert r["L37"].verdict == "pass" and r["L05"].verdict == "pass"
    assert r["L37"].detail["links"][0] == {"link": "pcie0", "expected": "A Ethernet31 (8/4)",
                                           "seen": "A Ethernet31", "ifname": "ens3f0np0"}
    assert r["L05"].detail["links"][1]["ifname"] == "ens3f1np1"


def test_swapped_wedges_fail_naming_both():
    g = geo({"pcie0": "A:8/3", "pcie1": "B:8/4"})
    lldp = good()
    lldp["A"][0] = E("Ethernet31", "rr101-l37", "88:e9:a4:00:00:02", "ens3f1np1")
    lldp["B"][0] = E("Ethernet31", "rr101-l37", "88:e9:a4:00:00:01")
    r = check(g, lldp, MACS)["L37"]
    assert r.verdict == "fail"
    assert "pcie0: expected A Ethernet30 (8/3), seen" in r.detail["error"]


def test_one_port_unseen_fails():
    lldp = good()
    lldp["B"] = [x for x in lldp["B"] if "l37" not in x.remote_device]
    r = check(geo(), lldp, MACS)
    assert r["L37"].verdict == "fail" and "pcie1: expected B Ethernet31 (8/4), not seen on any wedge" in r["L37"].detail["error"]
    assert r["L05"].verdict == "pass"


def test_both_ports_on_wedge_a_fails_pcie1():
    lldp = good()
    lldp["B"] = [x for x in lldp["B"] if "l37" not in x.remote_device]
    lldp["A"].append(E("Ethernet30", "rr101-l37", "88:e9:a4:00:00:02", "ens3f1np1"))
    r = check(geo(), lldp, MACS)["L37"]
    assert r.verdict == "fail"
    assert r.detail["links"][0]["seen"] == "A Ethernet31"
    assert "pcie1: expected B Ethernet31 (8/4), seen A Ethernet30" in r.detail["error"]
    assert r.detail["links"][1]["ifname"] == "ens3f1np1"


def test_hostname_fallback_when_no_macs():
    r = check(geo(), good(), {})
    assert r["L37"].verdict == "pass"
    assert r["L05"].verdict == "fail"          # "localhost" cannot match by name


def test_link_to_a_switch_missing_from_geometry_fails_that_link():
    """No guessing at Dell naming for an unknown switch: the link fails by name,
    the rest of the device is still judged."""
    from flax_rack.geometry import Link
    g = geo()
    d = next(x for x in g.devices if x.pos == "L37")
    d.links["pcie1"] = Link("Z", d.links["pcie1"].port, d.links["pcie1"].lane)
    r = check(g, good(), MACS)["L37"]
    assert r.verdict == "fail"
    assert "pcie1: switch Z not in geometry" in r.detail["error"]
    assert "pcie0" not in r.detail["error"]
    assert r.detail["links"][1] == {"link": "pcie1", "expected": None, "seen": None,
                                    "ifname": None}
