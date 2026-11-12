"""The drawer's PWR + IDNT (flax_rack.nodeact) over the real flax_rungs.bmc helpers and a
fake ipmitool: argv never carries the password, power is refused while a stage holds
the node, identify's state + expiry."""
import json, subprocess
from datetime import datetime, timedelta, timezone
import pytest
pytest.importorskip("flax_rack.nodeact")
from flax_rack import nodeact
from flax_rack.addressing import plan_addresses
from flax_rack.creds import Creds
from flax_rack.server import App, DEFAULT_STATIC
from flax_rack.stages import OrderContext
from flax_rungs import bmc
from tests.test_flax_rack_server import _order_dir, _start, get_json, post
from tests.test_flax_rack_stages import FakeKea

PW = "s3cret-bmc"


class Ipmi:
    """ipmitool answering on cipher 17; records argv + the IPMI_PASSWORD it saw."""
    def __init__(self, ident="Temporary(On)"):
        self.calls, self.ident, self.power = [], ident, "on"

    def __call__(self, argv, **kw):
        assert argv[0] == "ipmitool" and PW not in " ".join(argv) and "-P" not in argv
        assert "-E" in argv and kw["env"]["IPMI_PASSWORD"] == PW
        cmd = argv[argv.index("-E") + 1:]
        self.calls.append(cmd)
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        if argv[argv.index("-C") + 1] != "17":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="RAKP 2 error")
        if cmd[:3] == ["chassis", "power", "status"]:
            return ok(f"Chassis Power is {self.power}\n")
        if cmd[:2] == ["chassis", "power"]:
            self.power = "off" if cmd[2] == "off" else "on"
            return ok(f"Chassis Power Control: {cmd[2]}\n")
        if cmd == ["chassis", "status"]:
            return ok(f"System Power         : {self.power}\n"
                      + (f"Chassis Identify State : {self.ident}\n" if self.ident else ""))
        if cmd[:2] == ["chassis", "identify"]:
            return ok("Chassis identify interval: 240 seconds\n")
        raise AssertionError(cmd)


@pytest.fixture
def rig(tmp_path):
    _order_dir(tmp_path)
    ipmi = Ipmi()

    def factory(rack_dir, g, db):
        return OrderContext(rack_dir, g, db, FakeKea(), Creds({"sonicuser": "u", "sonicpass": "p"}, [("admin", PW)]), ipmi)
    app = App(tmp_path, DEFAULT_STATIC, factory)
    app.rescan()
    return app, app.rack("rr101"), ipmi


def bmc_ip(r, pos):
    return next(a.ip for a in plan_addresses(r.g) if a.pos == pos and a.role == "bmc")


def test_power_on_off_events_and_live_chip(rig):
    app, r, ipmi = rig
    assert app.node_power(r, {"pos": "L37", "action": "off"})[0] == 400        # no confirm
    assert app.node_power(r, {"pos": "L37", "action": "off", "confirm": "R37"})[0] == 400
    assert app.node_power(r, {"pos": "JBOD01", "action": "on"})[0] == 404      # hosts only
    assert app.node_power(r, {"pos": "L37", "action": "zap"})[0] == 400
    code, out = app.node_power(r, {"pos": "L37", "action": "off", "confirm": "L37"}, who="ui 10.0.0.9")
    assert code == 200 and out["power"] == "off" and out["cipher"] == 17
    assert ["chassis", "power", "off"] in ipmi.calls
    live = {d["pos"]: d["bmc_live"] for d in r.db.devices()}["L37"]
    assert live["power"] == "off" and live["reachable"] is True
    code, out = app.node_power(r, {"pos": "L37", "action": "reset", "confirm": "L37"})
    assert code == 200 and ["chassis", "power", "reset"] in ipmi.calls
    ev = [e["text"] for e in r.db.events(5)]
    assert ev[1].startswith("L37 power off by ui 10.0.0.9: ok (cipher 17; power now off)")
    assert all(PW not in e for e in ev)


def test_power_refused_while_a_stage_holds_the_node(rig):
    app, r, ipmi = rig
    assert r.lock.acquire()
    try:
        r.stage, r.stage_positions = "qualify", ["R05"]
        code, out = app.node_power(r, {"pos": "R05", "action": "on"})
        assert code == 409 and "qualify is running for R05" in out["error"]
        assert app.node_power(r, {"pos": "L37", "action": "on"})[0] == 200    # not in its scope
        r.stage, r.stage_positions = "run", None                              # rack-wide
        code, out = app.node_power(r, {"pos": "L37", "action": "cycle", "confirm": "L37"})
        assert code == 409 and "run is running for the rack" in out["error"]
        # identify is an LED, never refused by the lock
        assert app.node_identify(r, {"pos": "L37", "mode": "on"})[0] == 200
    finally:
        r.lock.release()
    assert [c for c in ipmi.calls if c[:2] == ["chassis", "power"] and c[2] != "status"] == \
        [["chassis", "power", "on"]]


def test_no_stage_starts_inside_a_power_call(rig, monkeypatch):
    app, r, _ = rig
    seen = {}

    def power(ctx, pos, ip, action, who):
        if pos != "L37":
            return 200, {}
        seen["stage"] = app._start_stage(r, "discover", lambda c: None)
        seen["other"] = app.node_power(r, {"pos": "R37", "action": "on"})[0]
        seen["same"] = app.node_power(r, {"pos": pos, "action": "on"})
        return 200, {}
    monkeypatch.setattr(nodeact, "power", power)
    assert app.node_power(r, {"pos": "L37", "action": "on"})[0] == 200
    assert seen["stage"][0] == 409 and "(power)" in seen["stage"][1]["error"]
    assert seen["other"] == 200                       # another node's PWR shares the lock
    assert seen["same"][0] == 409 and "in flight for L37" in seen["same"][1]["error"]
    assert not r.lock.locked() and not r.powering


def test_qualify_scope_covers_the_iperf_server(rig):
    app, r, _ = rig
    gate = __import__("threading").Event()
    app._start_stage(r, "qualify", lambda ctx, **kw: gate.wait(5), positions=["L37"])
    try:
        assert r.stage_positions == sorted({"L37", r.g.iperf_server})
        assert app.holds(r, r.g.iperf_server) and not app.holds(r, "R37")
    finally:
        gate.set()


def test_identify_modes_state_and_expiry(rig):
    app, r, ipmi = rig
    code, out = app.node_identify(r, {"pos": "L37", "mode": "on"}, who="ui x")
    assert code == 200 and ipmi.calls[-2] == ["chassis", "identify", "240"]
    st = out["ident"]
    assert st["on"] and st["mode"] == "on" and st["reported"] == "Temporary(On)"
    later = datetime.fromisoformat(st["until"]) + timedelta(seconds=1)
    assert nodeact.ident_view(r.db.idents()["L37"], later)["on"] is False      # expired
    app.node_identify(r, {"pos": "L37", "mode": "force"})
    assert ipmi.calls[-2] == ["chassis", "identify", "force"]
    assert nodeact.ident_view(r.db.idents()["L37"], later + timedelta(days=1))["on"] is True
    ipmi.ident = None                                  # a BMC that does not report it
    code, out = app.node_identify(r, {"pos": "L37", "mode": "off"})
    assert ipmi.calls[-2] == ["chassis", "identify", "0"] and out["ident"]["on"] is False
    assert out["ident"]["reported"] is None
    assert r.db.events(1)[0]["text"] == "L37 identify off by ?: ok"


def test_bmc_reset_off_rereads_power(tmp_path):
    ipmi = Ipmi()
    assert bmc.power_action("10.0.0.1", [("admin", PW)], "reset", runner=ipmi)
    assert bmc.parse_chassis_status("System Power : off\nChassis Identify State : Off\n") == \
        {"power": "off", "ident": "Off"}


def test_routes_and_state(tmp_path):
    _order_dir(tmp_path)
    ipmi = Ipmi()
    srv = _start(tmp_path, lambda d, g, db: OrderContext(d, g, db, FakeKea(),
                                                       Creds({"sonicuser": "u", "sonicpass": "p"}, [("admin", PW)]), ipmi))
    try:
        r = post(srv, "/rr101/api/identify", {"pos": "C37", "mode": "force"})
        assert r.status == 200
        r = post(srv, "/rr101/api/power", {"pos": "C37", "action": "cycle", "confirm": "C37"})
        assert r.status == 200 and json.loads(r.body)["power"] == "on"
        s = get_json(srv, "/rr101/api/state")
        c37 = next(d for d in s["devices"] if d["pos"] == "C37")
        assert c37["ident"]["on"] and c37["ident"]["mode"] == "force"
        assert s["stage_positions"] is None
    finally:
        srv.shutdown()
