"""Lab racks: Place / Unplace / Move hardware into BOM slots (plan 2026-11-02 Task 5)."""
import json
import pytest
pytest.importorskip("flax_rack.server")
from flax_rack.orderdb import OrderDB
from flax_rack.geometry import load_geometry
from tests.test_flax_rack_geometry import base, dev
from tests.test_flax_rack_server import _start, Factory, get_json, post

UNPLACED = [
    {"switch": "H", "port": 18, "lane": 1, "host_mac": "0c42a1ecbc0e", "bmc_mac": "0c42a1ecbc11",
     "host_ip": "172.31.0.58", "bmc_ip": "172.31.0.54", "macs": ["0c42a1ecbc0e", "0c42a1ecbc11"],
     "product": "Tioga Pass Single Side", "serial": "QTFCKQ024030B", "family": "tiogapass"},
    {"switch": "H", "port": 5, "lane": 1, "host_mac": "98039ba8f0ee", "bmc_mac": "98039ba8f0f0",
     "host_ip": "172.31.0.53", "bmc_ip": "172.31.0.50", "macs": ["98039ba8f0ee", "98039ba8f0f0"],
     "product": "Leopard ORv2-DDR4", "serial": "QCFF061921023D", "family": "leopard"},
    {"switch": "H", "port": 20, "lane": 1, "host_mac": "aabbccddeeff", "bmc_mac": None,
     "host_ip": None, "bmc_ip": None, "macs": ["aabbccddeeff"], "product": "", "serial": "",
     "family": ""},
]


@pytest.fixture
def lab(tmp_path):
    d = tmp_path / "howlin"
    d.mkdir()
    geo = base(lab=True, devices=[dev("C18", "bb-head", drives="BB20"),
                                  dev("BB20", "bigbasin", ou=20), dev("L31", "compute-tp"),
                                  dev("L35", "compute-leopard")])
    (d / "geometry.json").write_text(json.dumps(geo))
    (d / ".rackpost").touch()
    db = OrderDB(d / "order.db"); db.init(load_geometry(d / "geometry.json"))
    db.set_unplaced(UNPLACED)
    s = _start(tmp_path, Factory())
    yield s, db
    s.shutdown(); s.server_close()


def state(s):
    return get_json(s, "/howlin/api/state")


def test_state_absent_and_unplaced(lab):
    s, _ = lab
    st = state(s)
    assert st["lab"] is True and len(st["unplaced"]) == 3
    assert st["unplaced"][0]["port_label"] == "Et18/1"
    assert {d["pos"]: d["absent"] for d in st["devices"]} == {"C18": True, "BB20": True,
                                                              "L31": True, "L35": True}


def test_place_marks_present_and_records_event(lab):
    s, db = lab
    r = post(s, "/howlin/api/place", {"pos": "C18", "port": 18, "lane": 1})
    assert r.status == 200, r.body
    st = state(s)
    c18 = next(d for d in st["devices"] if d["pos"] == "C18")
    assert c18["absent"] is False and c18["placement"]["serial"] == "QTFCKQ024030B"
    assert c18["placement"]["port_label"] == "Et18/1" and c18["deviation"] is None
    assert [e["port"] for e in st["unplaced"]] == [5, 20]
    assert "place" in [e["kind"] for e in db.events_all()]


def test_place_deviation_text(lab):
    s, _ = lab
    assert post(s, "/howlin/api/place", {"pos": "L31", "port": 5, "lane": 1}).status == 200
    l31 = next(d for d in state(s)["devices"] if d["pos"] == "L31")
    assert l31["deviation"] == "kind: leopard in a compute-tp slot"


def test_place_refusals(lab):
    s, _ = lab
    assert post(s, "/howlin/api/place", {"pos": "C18", "port": 20, "lane": 1}).status == 409  # no FRU
    assert post(s, "/howlin/api/place", {"pos": "NOPE", "port": 18, "lane": 1}).status == 400
    assert post(s, "/howlin/api/place", {"pos": "C18", "port": 18, "lane": 1}).status == 200
    assert post(s, "/howlin/api/place", {"pos": "C18", "port": 5, "lane": 1}).status == 409   # occupied


def test_move_and_unplace(lab):
    s, db = lab
    post(s, "/howlin/api/place", {"pos": "C18", "port": 18, "lane": 1})
    post(s, "/howlin/api/place", {"pos": "L35", "port": 5, "lane": 1})
    assert post(s, "/howlin/api/move", {"pos": "C18", "to": "L35"}).status == 409
    assert post(s, "/howlin/api/move", {"pos": "C18", "to": "L31"}).status == 200
    assert set(db.placements()) == {"L31", "L35"}
    assert post(s, "/howlin/api/unplace", {"pos": "L35"}).status == 200
    st = state(s)
    assert set(db.placements()) == {"L31"} and 5 in [e["port"] for e in st["unplaced"]]


def test_place_refused_on_an_order(tmp_path):
    from tests.test_flax_rack_server import _order_dir
    _order_dir(tmp_path)
    s = _start(tmp_path, Factory())
    try:
        assert post(s, "/rr101/api/place", {"pos": "L37", "port": 7, "lane": None}).status == 409
    finally:
        s.shutdown(); s.server_close()


def test_process_report_lists_placement_and_deviation(lab, tmp_path):
    s, db = lab
    post(s, "/howlin/api/place", {"pos": "L31", "port": 5, "lane": 1})
    from flax_rack import process_report
    g = load_geometry(tmp_path / "howlin" / "geometry.json")
    data = process_report.build(tmp_path / "howlin", g, db)
    assert data["deviations"] == [{"pos": "L31", "text": "kind: leopard in a compute-tp slot"}]
    acts = [e["kind"] for p in data["passes"] for e in p["actions"]]
    assert "place" in acts
    html = process_report.render(data)
    assert "deviations (hardware vs its BOM slot)" in html and "kind: leopard in a compute-tp slot" in html


def test_rack_verdicts_judge_present_only(lab):
    s, db = lab
    st = state(s)
    assert st["qualify"]["not_passed"] == [] and st["present"] == 0 and st["total"] == 4
    post(s, "/howlin/api/place", {"pos": "C18", "port": 18, "lane": 1})
    st = state(s)
    # C18 and the Big Basin it drives (judged through C18) are present now
    assert st["qualify"]["not_passed"] == ["C18", "BB20"] and (st["present"], st["total"]) == (2, 4)


def test_pollers_see_placed_ports_not_absent_slots(lab, tmp_path):
    s, db = lab
    post(s, "/howlin/api/place", {"pos": "C18", "port": 18, "lane": 1})
    from flax_rack.server import poll_ctx
    from flax_rack.geometry import Link
    app = s.app
    r = app.rack("howlin")
    ctx = poll_ctx(app.factory, r)
    assert [d.pos for d in ctx.g.devices] == ["C18", "BB20"]      # absent L31/L35 not polled
    assert ctx.g.device("C18").links["mezz0"] == Link("H", 18, 1)


@pytest.fixture
def labjbod(tmp_path):
    d = tmp_path / "howlin"
    d.mkdir()
    geo = base(lab=True, devices=[dev("C05", "bc-head", drives="BC09"),
                                  dev("BC09", "bc-jbod", ou=9, expect={"disks": 4})])
    (d / "geometry.json").write_text(json.dumps(geo))
    (d / ".rackpost").touch()
    db = OrderDB(d / "order.db"); db.init(load_geometry(d / "geometry.json"))
    db.set_unplaced([{"switch": "H", "port": 9, "lane": 1, "host_mac": "b026285de309",
                      "bmc_mac": None, "host_ip": "172.31.0.70", "bmc_ip": None,
                      "macs": ["b026285de309"], "product": "", "serial": "", "family": ""}])
    s = _start(tmp_path, Factory())
    yield s, db
    s.shutdown(); s.server_close()


def test_place_an_iom(labjbod):
    s, db = labjbod
    st = state(s)
    assert "BC09.iom1" in st["iom_slots"] and "BC09.iom2" in st["iom_slots"]
    r = post(s, "/howlin/api/place", {"pos": "BC09.iom1", "port": 9, "lane": 1})
    assert r.status == 200, r.body
    p = db.placements()["BC09.iom1"]
    assert (p["bmc_mac"], p["host_mac"], p["port"]) == ("b026285de309", None, 9)
    bc = next(d for d in state(s)["devices"] if d["pos"] == "BC09")
    assert bc["absent"] is False
    assert post(s, "/howlin/api/place", {"pos": "BC09.iom3", "port": 9, "lane": 1}).status == 400


def test_unplace_an_iom(labjbod):
    s, db = labjbod
    post(s, "/howlin/api/place", {"pos": "BC09.iom1", "port": 9, "lane": 1})
    assert post(s, "/howlin/api/unplace", {"pos": "BC09.iom1"}).status == 200
    assert db.placements() == {}


def test_lab_rack_shows_unplaced_not_unexpected(lab):
    s, db = lab
    from flax_rungs.result import RungResult
    rid = db.start_run("_rack", "discover", "slot-match")
    db.finish_run(rid, RungResult("pass", {"unexpected": [{"port": 18, "mac": "0c42a1ecbc0e"}]}))
    st = state(s)
    assert st["unexpected"] == [] and len(st["unplaced"]) == 3



def test_unexpected_ports_are_ints_on_lanes():
    from flax_rack.discover import match
    from flax_rack.geometry import parse_geometry
    from flax_rack.switches import FdbEntry
    g = parse_geometry(base(lab=True, devices=[dev("L31", "compute-tp")]))
    m = match(g, [FdbEntry(1, "0c42a1ecbc0e", "Ethernet18/1")], "H")
    assert m.unexpected == [(18, "0c42a1ecbc0e")]
