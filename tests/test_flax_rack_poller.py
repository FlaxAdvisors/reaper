import sqlite3, subprocess, threading, time
import pytest
pytest.importorskip("flax_rack.poller")
from flax_rack.creds import Creds, CredsError
from flax_rack.geometry import load_geometry
from flax_rack.orderdb import OrderDB
from flax_rack.poller import BmcPoller, poll_once
from flax_rack.server import make_server
from flax_rack.stages import OrderContext
from tests.test_flax_rack_geometry import FIX
from tests.test_flax_rack_server import _order_dir, get_json
from tests.test_flax_rack_stages import FakeKea

DEAD = "172.32.37.61"        # C37's BMC


def runner(argv, **kw):
    cp = lambda rc=0, out="": subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
    if argv[0] == "ping":
        return cp(1 if argv[-1] == DEAD else 0)
    if argv[0] == "ipmitool":
        assert "-P" not in argv
        return cp(0, "Chassis Power is on\n")
    raise AssertionError(argv)


def ctx_for(tmp_path, creds=None):
    g = load_geometry(FIX)
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    return OrderContext(tmp_path, g, db, FakeKea(), creds or Creds({"sonicuser": "u", "sonicpass": "p"}, [("bu", "bp")]), runner)


def test_dead_bmc_unreachable_no_runs(tmp_path):
    c = ctx_for(tmp_path)
    assert poll_once(c) == 5                          # the JBOD has IOMs, no BMC
    live = {d["pos"]: d["bmc_live"] for d in c.db.devices()}
    assert live["C37"]["reachable"] is False and live["C37"]["power"] is None
    assert live["L37"]["reachable"] is True and live["L37"]["power"] == "on" and live["L37"]["at"]
    assert live["JBOD01"]["reachable"] is None
    assert c.db.latest_runs() == {}
    assert sqlite3.connect(tmp_path / "order.db").execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0


def test_missing_creds_pings_only(tmp_path):
    c = ctx_for(tmp_path)

    def boom():
        raise CredsError("no file")
    c.creds = boom
    poll_once(c)
    live = {d["pos"]: d["bmc_live"] for d in c.db.devices()}
    assert live["L37"] == {"reachable": True, "power": None, "at": live["L37"]["at"]}


def test_poller_thread_tolerates_exceptions_and_stops(tmp_path):
    n = {"calls": 0}
    c = ctx_for(tmp_path)

    def factory():
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("geometry mid-edit")
        return c
    p = BmcPoller(factory, interval_s=0.02)
    p.start()
    end = time.time() + 5
    while n["calls"] < 3 and time.time() < end:
        time.sleep(0.01)
    p.stop(); p.join(2)
    assert not p.is_alive() and n["calls"] >= 3
    assert next(d for d in c.db.devices() if d["pos"] == "L37")["bmc_live"]["power"] == "on"


def test_old_order_db_gains_columns(tmp_path):
    con = sqlite3.connect(tmp_path / "order.db")
    con.executescript("CREATE TABLE device(pos TEXT PRIMARY KEY, kind TEXT, host_mac TEXT, "
                      "bmc_mac TEXT, iom_macs TEXT NOT NULL DEFAULT '[]', pop_override TEXT);"
                      "INSERT INTO device(pos,kind) VALUES('L37','compute-tp');")
    con.commit(); con.close()
    db = OrderDB(tmp_path / "order.db"); db.init(load_geometry(FIX))
    db.set_bmc_live("L37", True, "off", "2026-10-29T00:00:00+00:00")
    assert next(d for d in db.devices() if d["pos"] == "L37")["bmc_live"]["power"] == "off"
    with pytest.raises(KeyError):
        db.set_bmc_live("Z99", True, None, "x")


def test_daemon_pollers_per_rack_stop_on_disable(tmp_path):
    a = _order_dir(tmp_path)
    b = _order_dir(tmp_path, "RRORD-101b", rack="b", prefix="rr101b-", vid=33)

    def factory(rack_dir, g, db):
        return OrderContext(rack_dir, g, db, FakeKea(), Creds({"sonicuser": "u", "sonicpass": "p"}, [("bu", "bp")]), runner)
    srv = make_server(tmp_path, port=0, context_factory=factory)
    app = srv.app
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        app.sync_pollers(interval_s=30)
        assert set(app._pollers) == {a, b} and all(p.is_alive() for p in app._pollers.values())
        pb = app._pollers[b]
        (b / ".rackpost").unlink()
        app.sync_pollers(interval_s=30)
        assert set(app._pollers) == {a}
        pb.join(2)
        assert not pb.is_alive()
        end = time.time() + 5
        while time.time() < end:
            d = next(x for x in get_json(srv, "/rr101/api/state")["devices"] if x["pos"] == "L37")
            if d["bmc_live"]["at"]:
                break
            time.sleep(0.02)
        assert d["bmc_live"]["reachable"] is True and d["bmc_live"]["power"] == "on"
        assert d["runs"] == {}
    finally:
        app.stop_pollers()
        srv.shutdown(); srv.server_close()


def test_ident_turned_off_elsewhere_shows_off(tmp_path):
    from flax_rack import nodeact
    st = {}
    c = ctx_for(tmp_path)

    def bmc(argv, **kw):
        if argv[0] == "ipmitool" and argv[-2:] == ["chassis", "status"]:
            return subprocess.CompletedProcess(argv, 0, stdout="System Power : on\n"
                                               f"Chassis Identify State : {st['now']}\n", stderr="")
        return runner(argv, **kw)
    c.runner = bmc
    c.db.set_ident("L37", {"mode": "force", "at": "2026-10-30T10:00:00+00:00", "until": None,
                           "reported": "Indefinite(On)", "who": "ui"})
    st["now"] = "Indefinite(On)"
    poll_once(c)
    assert nodeact.ident_view(c.db.idents()["L37"])["on"] is True
    st["now"] = "Off"                                   # someone ran `chassis identify 0`
    poll_once(c)
    assert nodeact.ident_view(c.db.idents()["L37"])["on"] is False
    assert "R37" not in c.db.idents()                  # no IDNT row: nothing read or added
