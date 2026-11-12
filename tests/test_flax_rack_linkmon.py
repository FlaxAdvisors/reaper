import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
pytest.importorskip("flax_rack.linkmon")
from flax_rack import linkmon
from flax_rack.creds import Creds
from flax_rack.fakes import with_wedge_ips
from flax_rack.geometry import load_geometry
from flax_rack.linkmon import LinkPoller, device_names, evaluate, presence
from flax_rack.orderdb import OrderDB
from flax_rack.stages import OrderContext
from flax_rungs.result import RungResult
from flax_rack.switches import IfStatus, LldpEntry, read_live, read_lldp
from flax_switch_sense.driver_sonic import _BATCH_MARK, SonicError
from tests.test_flax_rack_geometry import FIX
from tests.test_flax_rack_server import _order_dir, get_json, _start
from tests.test_flax_rack_stages import FakeKea

FIXD = Path(FIX).parent
NOW = 1_800_000_000.0
L37 = {"02fa25010010", "02fa25010012"}          # host, BMC
C37 = {"02fa25020010", "02fa25020012"}


def g():
    return load_geometry(FIX)


def snap(up=(), lldp=(), fdb=(), at=NOW, n=128):
    return {"at": at, "ifaces": {f"Ethernet{i}": IfStatus("up" if f"Ethernet{i}" in up else "down",
                                                         "up", "1G") for i in range(n)},
            "lldp": list(lldp), "fdb": list(fdb)}


def wedge_lldp():
    """The wedge's LLDP as the shared SonicDriver reads it (lldpctl JSON; the same rows
    as wedge-show-lldp-table.txt, the old `show lldp table` fixture)."""
    text = (FIXD / "wedge-lldpctl-fake.json").read_text()
    return [LldpEntry(**e) for e in read_lldp("w", "sonic", {}, runner=lambda c: text).detail["lldp"]]


def fdb(mac, iface):
    return {"vlan": 1000, "mac": mac, "iface": iface}


def test_installed_node_lldp_name_is_ok():
    lldp = wedge_lldp()
    a = snap(up={"Ethernet31", "Ethernet108", "Ethernet109"}, lldp=lldp)
    r = evaluate(g(), {"A": a}, {}, device_names(g()), NOW)
    assert r["L37"]["pcie0"]["state"] == "ok" and "rr101-l37" in r["L37"]["pcie0"]["text"]
    assert r["L37"]["pcie0"]["seen"]["lldp"] == "rr101-l37"
    assert r["R05"]["pcie0"]["state"] == "ok"              # Ethernet109 rr101-r05
    assert r["L05"]["pcie0"]["state"] == "up"              # Ethernet108 localhost: no identity
    # the fixture BOM repeats A:8/4 for C37/R37: L37 seen there is not a wrong cable
    assert r["C37"]["pcie0"]["state"] == "up" and "L37" in r["C37"]["pcie0"]["text"]
    assert r["L37"]["pcie1"]["state"] == "unknown"         # B never read


def test_backup_port_up_is_up_not_missing():
    # active-backup: the backup fabric port is oper up but silent (no FDB, no LLDP)
    b = snap(up={"Ethernet31"})
    r = evaluate(g(), {"B": b}, {"L37": L37}, device_names(g()), NOW)
    assert r["L37"]["pcie1"]["state"] == "up"
    assert r["L37"]["pcie1"]["text"] == "Ethernet31 up · no identity seen"
    assert presence(r["L37"]) == "present"


def test_cable_on_wrong_port_is_wrong():
    # C37's mezz cable moved onto L37's port C:7 (Ethernet6); C37's own C:8 is dark
    c = snap(up={"Ethernet6"}, fdb=[fdb(m, "Ethernet6") for m in sorted(C37)], n=52)
    r = evaluate(g(), {"C": c}, {"L37": L37, "C37": C37}, device_names(g()), NOW)
    assert r["L37"]["mezz0"]["state"] == "wrong"
    assert "C37" in r["L37"]["mezz0"]["text"] and "02:fa:25:02:00:10" in r["L37"]["mezz0"]["text"]
    assert r["C37"]["mezz0"]["state"] == "down"
    # LLDP: R05's NIC answering on L05's lane
    a = snap(up={"Ethernet108"}, lldp=[LldpEntry("Ethernet108", "rr101-r05", "x", "ens3f0np0")])
    r = evaluate(g(), {"A": a}, {}, device_names(g()), NOW)
    assert r["L05"]["pcie0"]["state"] == "wrong" and "rr101-r05 (R05)" in r["L05"]["pcie0"]["text"]


def test_stale_snapshot_is_unknown():
    a = snap(up={"Ethernet31"}, at=NOW - linkmon.STALE_S + 1)
    assert evaluate(g(), {"A": a}, {}, device_names(g()), NOW)["L37"]["pcie0"]["state"] == "up"
    a = snap(up={"Ethernet31"}, at=NOW - linkmon.STALE_S - 1)
    r = evaluate(g(), {"A": a}, {}, device_names(g()), NOW)["L37"]["pcie0"]
    assert r["state"] == "unknown" and "91 s ago" in r["text"]


# ---------------------------------------------------------------- the poller
class Switches:
    """read_live stand-in: per switch id a snapshot detail, "down" (unmeasured) or an
    Event the read blocks on (a slow ssh)."""
    def __init__(self, geo, **by_id):
        self.ip = {s.mgmt_ip: s.id for s in geo.switches}
        self.by_id, self.calls = by_id, []

    def read_live(self, host, nos, cred, *, runner=None):
        sid = self.ip[host]
        self.calls.append(sid)
        v = self.by_id.get(sid, "down")
        if isinstance(v, threading.Event):
            v.wait(10)
            v = "down"
        if v == "down":
            return RungResult("unmeasured", {"headline": f"{host} did not answer ssh"})
        return RungResult("pass", {k: x for k, x in v.items() if k != "at"})


def ctx_for(tmp_path, sw, macs=True):
    geo = with_wedge_ips(g())
    db = OrderDB(tmp_path / "order.db"); db.init(geo)
    if macs:
        db.set_macs("L37", host_mac="02fa25010010", bmc_mac="02fa25010012", iom_macs=[])
    return OrderContext(tmp_path, geo, db, FakeKea(), Creds({"sonicuser": "u", "sonicpass": "p"}, [("bu", "bp")]),
                        None, sw)


def test_switch_unreachable_is_unknown_not_down(tmp_path):
    geo = with_wedge_ips(g())
    sw = Switches(geo, B=snap(up={"Ethernet31"}), C=snap(up={"Ethernet6"}, n=52,
                  fdb=[fdb(m, "Ethernet6") for m in L37]))     # A does not answer
    ctx = ctx_for(tmp_path, sw)
    p = LinkPoller(lambda: ctx, clock=lambda: NOW)
    p.tick(block=True)
    r = p.log_transitions(ctx)
    assert r["L37"]["pcie0"]["state"] == "unknown" and "A not answering" in r["L37"]["pcie0"]["text"]
    assert r["L37"]["pcie1"]["state"] == "up" and r["L37"]["mezz0"]["state"] == "ok"
    assert all(v["state"] == "unknown" for pos in r for v in r[pos].values() if v["switch"] == "A")
    assert p.switch_live("A") == {"at": None, "ok": False, "error": "fake-wedge-A did not answer ssh"}
    assert p.switch_live("B")["ok"] is True
    assert "down" not in {e["state"] for e in ctx.db.link_events() if e["pos"] == "L37"}


def test_slow_switch_one_read_in_flight(tmp_path):
    geo = with_wedge_ips(g())
    gate = threading.Event()
    sw = Switches(geo, A=gate, B=snap(up={"Ethernet31"}))
    ctx = ctx_for(tmp_path, sw)
    p = LinkPoller(lambda: ctx, clock=lambda: NOW)
    try:
        for _ in range(2):
            p.tick()
            for k in "BC":
                p._inflight[k].join(5)
        assert sw.calls.count("A") == 1 and sw.calls.count("B") >= 2
        assert p.log_transitions(ctx)["L37"]["pcie1"]["state"] == "up"     # B still updates
    finally:
        gate.set()


def test_a_read_is_judged_the_moment_it_lands(tmp_path):
    # no waiting for the next tick: the read thread itself evaluates and logs
    geo = with_wedge_ips(g())
    sw = Switches(geo, C=snap(up={"Ethernet6"}, n=52, fdb=[fdb(m, "Ethernet6") for m in L37]))
    ctx = ctx_for(tmp_path, sw)
    p = LinkPoller(lambda: ctx, clock=lambda: NOW)
    c = next(s for s in ctx.g.switches if s.id == "C")
    p._read(ctx, c, linkmon.switch_ips(ctx)["C"])
    assert [e["state"] for e in ctx.db.link_events()
            if (e["pos"], e["link"]) == ("L37", "mezz0")] == ["ok"]


def test_transition_logged_once(tmp_path):
    geo = with_wedge_ips(g())
    c_up = snap(up={"Ethernet6"}, n=52, fdb=[fdb(m, "Ethernet6") for m in L37])
    sw = Switches(geo, C=c_up)
    ctx = ctx_for(tmp_path, sw)
    p = LinkPoller(lambda: ctx, clock=lambda: NOW)
    for _ in range(3):
        p.tick(block=True)
    rows = [e for e in ctx.db.link_events() if (e["pos"], e["link"]) == ("L37", "mezz0")]
    assert [e["state"] for e in rows] == ["ok"]
    sw.by_id["C"] = snap(n=52)                                  # the cable is pulled
    p.tick(block=True); p.tick(block=True)
    rows = [e for e in ctx.db.link_events() if (e["pos"], e["link"]) == ("L37", "mezz0")]
    assert [e["state"] for e in rows] == ["down", "ok"]
    # one order event per switch per poll; the pull is in it, once
    assert sum("L37 mezz0 ok → down" in e["text"] for e in ctx.db.events(500)) == 1
    # a restarted daemon: nothing re-logged for unchanged links
    n = len(ctx.db.link_events(1000))
    p2 = LinkPoller(lambda: ctx, clock=lambda: NOW)
    p2.tick(block=True)
    assert len(ctx.db.link_events(1000)) == n
    assert ctx.db.latest_runs() == {}                          # live state is never a run


def test_one_event_per_switch_and_none_for_green_flaps(tmp_path):
    geo = with_wedge_ips(g())
    sw = Switches(geo, A=snap(up={"Ethernet31"}), C=snap(up={"Ethernet6"}, n=52,
                  fdb=[fdb(m, "Ethernet6") for m in L37]))
    ctx = ctx_for(tmp_path, sw)
    p = LinkPoller(lambda: ctx, clock=lambda: NOW)
    p.tick(block=True)
    link_ev = lambda: [e for e in ctx.db.events(500) if e["kind"] == "link"]   # noqa: E731
    n_rows, n_ev = len(ctx.db.link_events(1000)), len(link_ev())
    assert n_ev <= 3                                  # A, B, C: at most one each
    sw.by_id["C"] = snap(up={"Ethernet6"}, n=52)      # FDB aged out: mezz0 ok -> up
    p.tick(block=True)
    rows = [e["state"] for e in ctx.db.link_events() if (e["pos"], e["link"]) == ("L37", "mezz0")]
    assert rows[0] == "up" and len(ctx.db.link_events(1000)) > n_rows
    assert len(link_ev()) == n_ev                     # green <-> green: link_event only
    sw.by_id["A"] = "down"                            # A stops answering: one summary
    p.tick(block=True)
    new = link_ev()[:len(link_ev()) - n_ev]
    assert len(new) == 1 and new[0]["text"].startswith("switch A not read: ")


def test_wedge_ips_survive_start_fresh(tmp_path):
    """The wedges have no geometry mgmt_ip: their IP is Discover's lease, a fact that
    Start fresh does not hide (the links stay evaluated, not 'no mgmt IP')."""
    geo = g()
    assert not {s.id: s.mgmt_ip for s in geo.switches}["A"]
    db = OrderDB(tmp_path / "order.db"); db.init(geo)
    db.set_macs("L37", host_mac="02fa25010010", bmc_mac="02fa25010012", iom_macs=[])
    for sid, ip in (("A", "10.9.0.1"), ("B", "10.9.0.2")):
        rid = db.start_run(f"_sw-{sid}", "discover", "host-leased")
        db.finish_run(rid, RungResult("pass", {"ip": ip}))
    db.start_pass("op", "again")
    sw = Switches(geo, A=snap(up={"Ethernet31"}), B=snap(up={"Ethernet31"}))
    sw.ip.update({"10.9.0.1": "A", "10.9.0.2": "B"})
    ctx = OrderContext(tmp_path, geo, db, FakeKea(), Creds({"sonicuser": "u", "sonicpass": "p"}, [("bu", "bp")]), None, sw)
    assert linkmon.switch_ips(ctx)["A"] == "10.9.0.1"
    p = LinkPoller(lambda: ctx, clock=lambda: NOW)
    p.tick(block=True)
    r = p.log_transitions(ctx)
    assert r["L37"]["pcie0"]["state"] == "up", r["L37"]["pcie0"]
    assert "A" in sw.calls and "B" in sw.calls


def test_read_live_one_session():
    calls = []
    out = (f"{_BATCH_MARK}ifstatus\n"
           "  Interface  Lanes  Speed  MTU  FEC  Alias  Vlan  Oper  Admin  Type  Asym PFC\n"
           "-----------  -----  -----  ---  ---  -----  ----  ----  -----  ----  --------\n"
           "  Ethernet6      7     1G  9100  N/A  a  trunk  up  up  N/A  N/A\n"
           f"{_BATCH_MARK}mac\n" + (FIXD / "dell-show-mac.txt").read_text()
           + f"{_BATCH_MARK}lldp\n" + (FIXD / "wedge-lldpctl-fake.json").read_text()
           + f"{_BATCH_MARK}end\n")
    r = read_live("10.0.0.1", "sonic", {}, runner=lambda c: calls.append(c) or out)
    assert len(calls) == 1
    assert r.verdict == "pass" and r.detail["ifaces"]["Ethernet6"].oper == "up"
    assert len(r.detail["lldp"]) == 3 and r.detail["fdb"][0].iface == "Ethernet6"

    def dead(cmd):
        raise SonicError("10.0.0.1: ssh exited 255: no route", 255)
    assert read_live("10.0.0.1", "sonic", {}, runner=dead).verdict == "unmeasured"


def test_api_state_live_fields(tmp_path):
    _order_dir(tmp_path)
    srv = _start(tmp_path, lambda d, geo, db: None)
    try:
        st = get_json(srv, "/rr101/api/state")
    finally:
        srv.shutdown(); srv.server_close()
    d = next(x for x in st["devices"] if x["pos"] == "L37")
    assert d["presence"] == "unknown" and d["live_links"]["mezz0"]["state"] == "unknown"
    sw = next(x for x in st["switches"] if x["id"] == "C")
    assert sw["live"] == {"at": None, "ok": False, "error": None}
    assert sw["live_links"]["uplink1"]["state"] == "unknown"
    assert st["link_interval_s"] == 30
