import pathlib
import pytest
pytest.importorskip("flax_rack")
from flax_rack.discover import match
from flax_rack.geometry import load_geometry
from flax_rack.switches import FdbEntry, read_fdb
from tests.test_flax_rack_geometry import FIX
F = pathlib.Path(__file__).parent / "fixtures/rackpost"


def parse_show_mac(text):
    """The FDB the shared SonicDriver reads from `show mac` output."""
    return [FdbEntry(**e) for e in read_fdb("h", "sonic", {}, runner=lambda c: text).detail["fdb"]]

def _m(extra=()):
    fdb = parse_show_mac((F / "dell-show-mac.txt").read_text()) + list(extra)
    return match(load_geometry(FIX), fdb)

def test_rr101_all_found():
    m = _m()
    assert m.devices["L37"].status == "found"
    assert (m.devices["L37"].host_mac, m.devices["L37"].bmc_mac) == ("0c42a1e16b50", "0c42a1e16b52")
    assert m.devices["JBOD01"].iom_macs == ["98039b750996", "98039bc50d92"]
    assert m.unexpected == []            # Ethernet0 (port 1) is the uplink

def test_third_mac_makes_ambiguous_not_guessed():
    m = _m([FdbEntry(1000, "aabbccddeeff", "Ethernet7")])     # port 8 = C37
    assert m.devices["C37"].status == "ambiguous" and m.devices["C37"].bmc_mac is None

def test_mac_on_unassigned_port_is_unexpected():
    assert (20, "aabbccddeeff") in _m([FdbEntry(1000, "aabbccddeeff", "Ethernet19")]).unexpected

def test_missing_device():
    fdb = [e for e in parse_show_mac((F / "dell-show-mac.txt").read_text()) if e.iface != "Ethernet40"]
    assert match(load_geometry(FIX), fdb).devices["R37"].status == "missing"

def test_uplink_macs_ignored():
    assert _m([FdbEntry(1000, "aabbccddeeff", "Ethernet0")]).unexpected == []

def test_non_adjacent_pair_ambiguous():
    m = _m([FdbEntry(1000, "0c42a1e16b99", "Ethernet6")])
    assert m.devices["L37"].status == "ambiguous"

def test_iom_two_macs_ambiguous():
    m = _m([FdbEntry(1000, "aabbccddeeff", "Ethernet12")])
    assert m.devices["JBOD01"].status == "ambiguous" and m.devices["JBOD01"].iom_macs == []

def _only(drop_mac):
    return [e for e in parse_show_mac((F / "dell-show-mac.txt").read_text()) if e.mac != drop_mac]

def test_single_mac_is_bmc_host_derived():
    """Powered-off node: only the BMC (host+2) is in the FDB."""
    m = match(load_geometry(FIX), _only("0c42a1e16b50")).devices["L37"]
    assert m.status == "found"
    assert (m.host_mac, m.bmc_mac) == ("0c42a1e16b50", "0c42a1e16b52")
    assert m.note == "host derived (host MAC not seen)"

def test_single_mac_derivation_borrows_across_bytes():
    fdb = [e for e in _only("0c42a1e16b50") if e.iface != "Ethernet6"] + \
          [FdbEntry(1000, "0c42a1e20001", "Ethernet6")]
    m = match(load_geometry(FIX), fdb).devices["L37"]
    assert (m.status, m.host_mac, m.bmc_mac) == ("found", "0c42a1e1ffff", "0c42a1e20001")

def test_exact_non_adjacent_pair_still_ambiguous():
    fdb = [e for e in _only("0c42a1e16b50")] + [FdbEntry(1000, "0c42a1e16b99", "Ethernet6")]
    m = match(load_geometry(FIX), fdb).devices["L37"]
    assert m.status == "ambiguous" and m.host_mac is None and m.bmc_mac is None

def test_multi_host_without_matcher_is_unsupported_not_guessed():
    import json
    from flax_rack.geometry import parse_geometry
    d = json.loads(FIX.read_text())
    d["devices"][0]["endpoints"] = {"bmc": 1, "hosts": 4}      # L37 as a 4-card sled
    m = match(parse_geometry(d), parse_show_mac((F / "dell-show-mac.txt").read_text()))
    l37 = m.devices["L37"]
    assert l37.status == "unsupported" and l37.host_mac is None and l37.bmc_mac is None
    assert "no MAC matcher for kind compute-tp" in l37.note
    assert m.devices["C37"].status == "found"
    assert m.unexpected == []          # its port is still claimed, not reported stray

def test_matchers_registry_covers_every_kind():
    from flax_rack.discover import MATCHERS, match_host_bmc
    from flax_rack.geometry import KIND_ENDPOINTS
    assert set(MATCHERS) == set(KIND_ENDPOINTS)
    assert MATCHERS["compute-tp"] is MATCHERS["bc-head-tp"] is match_host_bmc


# ---------------------------------------------------------------- lab racks: lanes + placements
from flax_rack.discover import _port_macs
from flax_rack.geometry import parse_geometry, with_placements, absent, Switch, Link
from tests.test_flax_rack_geometry import base, dev

EOS_SW = Switch(id="H", role="mgmt", ou=39, model="arista", nos="eos")
TP = dict(serial="QTFCKQ024030B", product="Tioga Pass Single Side", switch="H", port=18,
          lane=1, host_mac="0c42a1ecbc0e", bmc_mac="0c42a1ecbc11")


def _lab():
    return parse_geometry(base(lab=True, devices=[dev("C18", "bb-head", drives="BB20"),
                                                  dev("BB20", "bigbasin", ou=20),
                                                  dev("L31", "compute-tp")]))


def test_port_macs_keeps_lanes():
    fdb = [FdbEntry(1, "98039ba8f0ee", "Ethernet5/1"), FdbEntry(1, "98039ba8f0f0", "Ethernet5/1"),
           FdbEntry(1, "506b4b24fc58", "Ethernet5/4"), FdbEntry(1, "506b4b24fc5a", "Ethernet5/4")]
    assert _port_macs(fdb, EOS_SW) == {(5, 1): ["98039ba8f0ee", "98039ba8f0f0"],
                                       (5, 4): ["506b4b24fc58", "506b4b24fc5a"]}


def test_with_placements_fills_mezz0_and_absent():
    g = _lab()
    eg = with_placements(g, {"C18": TP})
    assert eg.device("C18").links["mezz0"] == Link("H", 18, 1)
    assert g.device("C18").links == {}                      # the committed geometry is untouched
    assert absent(g, {"C18": TP}) == {"L31"}               # BB20 is judged through C18


def test_placed_match_by_recorded_macs_not_offset():
    g = with_placements(_lab(), {"C18": TP})          # host bc0e, BMC bc11: +3, not +2
    fdb = [FdbEntry(1, "0c42a1ecbc0e", "Ethernet18/1"), FdbEntry(1, "0c42a1ecbc11", "Ethernet18/1")]
    m = match(g, fdb, "H")
    c = m.devices["C18"]
    assert (c.status, c.host_mac, c.bmc_mac) == ("found", "0c42a1ecbc0e", "0c42a1ecbc11")
    assert m.unexpected == []


def test_discover_flags_serial_on_other_port():
    g = with_placements(_lab(), {"C18": TP})
    fdb = [FdbEntry(1, "0c42a1ecbc0e", "Ethernet20/1"), FdbEntry(1, "0c42a1ecbc11", "Ethernet20/1")]
    c = match(g, fdb, "H").devices["C18"]
    assert c.status == "moved" and "QTFCKQ024030B" in c.note and "Et20/1" in c.note


def test_other_hardware_on_a_placed_port_is_shown():
    from flax_rack.unplaced import find
    g = with_placements(_lab(), {"C18": TP})
    fdb = [FdbEntry(1, "98039ba8f0ee", "Ethernet18/1"), FdbEntry(1, "98039ba8f0f0", "Ethernet18/1")]
    c = match(g, fdb, "H").devices["C18"]
    assert c.status == "replaced" and "98039ba8f0f0" in c.note
    out = find(_port_macs(fdb, EOS_SW), {"C18": TP}, {}, lambda ip: None, uplinks=set())
    assert [(e["port"], e["lane"], e.get("on_slot")) for e in out] == [(18, 1, "C18")]


def test_placed_host_alone_on_its_port_is_found_not_replaced():
    g = with_placements(_lab(), {"C18": TP})
    c = match(g, [FdbEntry(1, "0c42a1ecbc0e", "Ethernet18/1")], "H").devices["C18"]   # BMC MAC aged out
    assert (c.status, c.host_mac, c.bmc_mac) == ("found", "0c42a1ecbc0e", "0c42a1ecbc11")
    assert "BMC MAC not seen" in c.note
