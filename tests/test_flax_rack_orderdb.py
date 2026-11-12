import pytest
pytest.importorskip("flax_rack")
from flax_rack.orderdb import OrderDB
from flax_rack.geometry import load_geometry
from flax_rungs.result import RungResult
from tests.test_flax_rack_geometry import FIX

@pytest.fixture
def db(tmp_path):
    d = OrderDB(tmp_path / "order.db"); d.init(load_geometry(FIX)); return d

def test_init_idempotent(db):
    db.init(load_geometry(FIX))
    assert sorted(x["pos"] for x in db.devices()) == ["C37","JBOD01","L05","L37","R05","R37"]

def test_runs_append_only_latest_wins(db):
    r1 = db.start_run("L37", "discover", "bmc-reach"); db.finish_run(r1, RungResult("fail", {"why": "x"}))
    r2 = db.start_run("L37", "discover", "bmc-reach"); db.finish_run(r2, RungResult("pass", {}))
    lr = db.latest_runs()[("L37", "bmc-reach")]
    assert lr["verdict"] == "pass" and lr["status"] == "done"
    assert db.run_count("L37", "bmc-reach") == 2

def test_restart_marks_running_interrupted(tmp_path):
    p = tmp_path / "order.db"; d = OrderDB(p); d.init(load_geometry(FIX))
    d.start_run("C37", "discover", "bmc-reach")
    d2 = OrderDB(p)
    assert d2.mark_interrupted() == 1
    assert d2.latest_runs()[("C37", "bmc-reach")]["status"] == "interrupted"

def test_pop_override_and_reset(db):
    db.set_pop("R37", "ovh-bare-tp")
    assert {x["pos"]: x["pop_override"] for x in db.devices()}["R37"] == "ovh-bare-tp"
    db.set_pop("R37", None)
    assert {x["pos"]: x["pop_override"] for x in db.devices()}["R37"] is None

def test_macs_normalised(db):
    db.set_macs("L37", host_mac="0C:42:A1:E1:6B:50", bmc_mac="0c42.a1e1.6b52", iom_macs=[])
    row = {x["pos"]: x for x in db.devices()}["L37"]
    assert (row["host_mac"], row["bmc_mac"]) == ("0c42a1e16b50", "0c42a1e16b52")

def test_finished_run_not_rewritten_and_extras(db):
    r = db.start_run("L37", "discover", "x"); db.finish_run(r, RungResult("pass", {}))
    db.finish_run(r, RungResult("fail", {}))
    assert db.latest_runs()[("L37", "x")]["verdict"] == "pass"
    assert db.mark_interrupted() == 0
    db.add_event("a", "one"); db.add_event("b", "two")
    assert [e["text"] for e in db.events()] == ["two", "one"]
    assert db.closed_at() is None
    db.set_closed(); t = db.closed_at(); db.set_closed()
    assert t and db.closed_at() == t


# ---------------------------------------------------------------- placement (lab racks)
def _pl(db, pos="R37", **kw):
    a = dict(serial="QTFCKQ024030B", product="Tioga Pass Single Side", switch="H", port=18,
             lane=1, host_mac="0c42a1ecbc0e", bmc_mac="0c42a1ecbc11", who="op")
    a.update(kw)
    db.place(pos, **a)


def test_placement_survives_start_pass(db):
    _pl(db)
    db.start_pass("op", "fresh")
    p = db.placements()["R37"]
    assert (p["serial"], p["port"], p["lane"], p["bmc_mac"]) == ("QTFCKQ024030B", 18, 1, "0c42a1ecbc11")
    assert [e["kind"] for e in db.events_all()][-2:] == ["place", "fresh"] or \
        "place" in [e["kind"] for e in db.events_all()]


def test_place_occupied_refused(db):
    _pl(db)
    with pytest.raises(ValueError, match="R37"):
        _pl(db, serial="X")


def test_unplace_and_move(db):
    _pl(db, "R37")
    _pl(db, "C37", serial="QCFF061921023D", port=5, host_mac="98039ba8f0ee", bmc_mac="98039ba8f0f0")
    with pytest.raises(ValueError, match="L37|occupied"):
        db.move("R37", "C37", "op")
    db.move("R37", "L37", "op")
    assert set(db.placements()) == {"L37", "C37"}
    db.unplace("C37", "op")
    assert set(db.placements()) == {"L37"}
    kinds = [e["kind"] for e in db.events_all()]
    assert {"place", "move", "unplace"} <= set(kinds)
