"""Lab racks: Snapshot archives a pass and keeps the rack open (plan 2026-11-02 Task 9)."""
import json
import pytest
pytest.importorskip("flax_rack.server")
from flax_rack.geometry import load_geometry, parse_geometry
from flax_rack.orderdb import OrderDB
from flax_rack.stages import OrderContext, snapshot
from tests.test_flax_rack_geometry import base, dev
from tests.test_flax_rack_server import _start, _order_dir, Factory, get_json, post
from tests.test_flax_rack_stages import CREDS, FakeKea, fake_runner


def _ctx(tmp_path):
    g = parse_geometry(base(lab=True, devices=[dev("L31", "compute-tp")]))
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    kea = FakeKea()
    kea.upsert("order:HOWLIN", "aabbccddeeff", "172.31.31.1", "howlin-l31", 31)
    return OrderContext(tmp_path, g, db, kea, CREDS, fake_runner, None)


def test_snapshot_keeps_reservations_and_open(tmp_path):
    c = _ctx(tmp_path)
    out = snapshot(c)
    assert c.db.closed_at() is None and len(c.kea.upserts) == 1 and c.kea.deletes == 0
    assert len(c.kea.archives) == 1 and out["pass"] == 1
    snap = tmp_path / "snapshots" / "pass-1"
    assert (snap / "process-report.html").is_file()
    assert "snapshot" in [e["kind"] for e in c.db.events_all()]


def test_snapshot_archive_carries_pass(tmp_path):
    c = _ctx(tmp_path)
    c.db.start_pass("op", "fresh")
    snapshot(c)
    p = c.kea.archives[-1]
    assert p["pass"] == 2 and p["snapshot"] is True


def test_close_refused_on_lab_rack(tmp_path):
    d = tmp_path / "howlin"
    d.mkdir()
    (d / "geometry.json").write_text(json.dumps(base(lab=True, devices=[dev("L31", "compute-tp")])))
    (d / ".rackpost").touch()
    s = _start(tmp_path, Factory())
    try:
        r = post(s, "/howlin/api/close", {"confirm": "HOWLIN"})
        assert r.status == 409 and b"Snapshot" in r.body
        r = post(s, "/howlin/api/snapshot", {"confirm": "HOWLIN"})
        assert r.status == 200, r.body
        assert get_json(s, "/howlin/api/state")["closed_at"] is None
    finally:
        s.shutdown(); s.server_close()


def test_snapshot_refused_on_an_order(tmp_path):
    _order_dir(tmp_path)
    s = _start(tmp_path, Factory())
    try:
        assert post(s, "/rr101/api/snapshot", {"confirm": "RRORD-101"}).status == 409
    finally:
        s.shutdown(); s.server_close()


def test_snapshot_archives_present_devices_only(tmp_path):
    g = parse_geometry(base(lab=True, devices=[dev("L31", "compute-tp"), dev("L33", "compute-tp")]))
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    db.place("L31", serial="S1", product="Tioga Pass Single Side", family="tiogapass", switch="H",
             port=31, lane=1, host_mac="0c42a1ecbc0e", bmc_mac="0c42a1ecbc10", who="op")
    c = OrderContext(tmp_path, g, db, FakeKea(), CREDS, fake_runner, None)
    snapshot(c)
    assert [d["pos"] for d in c.kea.archives[-1]["devices"] if not d["pos"].startswith("_sw-")] == ["L31"]
