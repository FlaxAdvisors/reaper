import re
import pytest
pytest.importorskip("flax_rack")
from flax_rack.unplaced import find, fru_summary

LEOPARD_FRU_TEXT = """FRU Device Description : Builtin FRU Device (ID 0)
 Chassis Type          : Rack Mount Chassis
 Chassis Serial        : QCFF061921023D
 Board Mfg             : Quanta
 Board Product         : Leopard ORv2-DDR4
 Board Serial          : QTF4KQ91101189
 Product Manufacturer  : Quanta
 Product Name          : Leopard ORv2-DDR4
 Product Part Number   : 1HY7UAZ0075
"""
FAMILY_MAP = {"leopard": [re.compile("Leopard ORv2-DDR4")],
              "tiogapass": [re.compile("^Tioga Pass( Single Side)?$")]}
TP_FRU = {"product": "Tioga Pass Single Side", "serial": "QTFCKQ024030B", "family": "tiogapass"}


def test_unplaced_pairs_by_fru_not_offset():
    by_port = {(18, 1): ["0c42a1ecbc0e", "0c42a1ecbc11"]}      # BMC = host+3
    leases = {"0c42a1ecbc0e": "172.31.0.58", "0c42a1ecbc11": "172.31.0.54"}
    out = find(by_port, {}, leases, lambda ip: TP_FRU if ip == "172.31.0.54" else None,
               uplinks={32})
    assert out == [{"switch": "H", "port": 18, "lane": 1, "host_mac": "0c42a1ecbc0e",
                    "bmc_mac": "0c42a1ecbc11", "host_ip": "172.31.0.58", "bmc_ip": "172.31.0.54",
                    "macs": ["0c42a1ecbc0e", "0c42a1ecbc11"], **TP_FRU}]


def test_placed_and_uplink_ports_not_unplaced():
    by_port = {(32, 1): ["ec0d9a91e96a"], (5, 1): ["98039ba8f0ee", "98039ba8f0f0"],
               (24, 1): ["ec0d9a74b4c8", "ec0d9a74b4ca"]}
    placements = {"L05": {"switch": "H", "port": 5, "lane": 1}}
    out = find(by_port, placements, {}, lambda ip: None, uplinks={32})
    assert [(e["port"], e["lane"]) for e in out] == [(24, 1)]


def test_no_fru_answer_is_listed_not_paired():
    out = find({(20, 1): ["aabbccddeeff"]}, {}, {}, lambda ip: None, uplinks=set())
    assert out[0]["bmc_mac"] is None and out[0]["product"] == "" and out[0]["macs"] == ["aabbccddeeff"]


def test_leopard_serial_is_chassis_serial():
    s = fru_summary(LEOPARD_FRU_TEXT, FAMILY_MAP)
    assert s == {"product": "Leopard ORv2-DDR4", "serial": "QCFF061921023D", "family": "leopard"}


# ---------------------------------------------------------------- Discover on a lab rack
from types import SimpleNamespace
from flax_rack.geometry import parse_geometry
from flax_rack.orderdb import OrderDB
from flax_rack.stages import OrderContext, run_discover
from flax_rungs.result import RungResult
from tests.test_flax_rack_geometry import base, dev
from tests.test_flax_rack_stages import CREDS, FakeKea, fake_runner

TP_FRU_TEXT = """FRU Device Description : Builtin FRU Device (ID 0)
 Board Product         : Tioga Pass Single Side
 Product Name          : Tioga Pass Single Side
 Product Serial        : QTFCKQ024030B
"""


def _lab_ctx(tmp_path):
    g = parse_geometry(base(lab=True, devices=[dev("C18", "bb-head", drives="BB20"),
                                               dev("BB20", "bigbasin", ou=20)]))
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    calls = []
    fdb = [{"vlan": 1, "mac": "0c42a1ecbc0e", "iface": "Ethernet18/1"},
           {"vlan": 1, "mac": "0c42a1ecbc11", "iface": "Ethernet18/1"},
           {"vlan": 1, "mac": "ec0d9a91e96a", "iface": "Ethernet32/1"}]

    def tp_fru(ip, creds, runner=None):
        calls.append(("tp_fru", ip))
        if ip == "172.31.0.54":
            return RungResult("pass", {"serial": "QTFCKQ024030B"}, {"fru0.txt": TP_FRU_TEXT})
        return RungResult("unmeasured", {"error": "no answer"}, {})

    def anything(name):
        def f(*a, **k):
            calls.append((name,) + a)
            return RungResult("fail", {"error": "not in this fake"})
        return f
    R = SimpleNamespace(read_fdb=lambda *a, **k: RungResult("pass", {"fdb": fdb, "count": 3}),
                        read_ifstatus=lambda *a, **k: RungResult("pass", {"status": {}}),
                        tp_fru=tp_fru, site_switches=lambda: {},
                        power_action=anything("power_action"), set_boot_pxe=anything("set_boot_pxe"),
                        ping=anything("ping"), wall=lambda: 0, clock=lambda: 0, sleep=lambda s: None)
    kea = FakeKea()
    kea.dyn.update({"0c42a1ecbc0e": "172.31.0.58", "0c42a1ecbc11": "172.31.0.54"})
    return OrderContext(tmp_path, g, db, kea, CREDS, fake_runner, R), calls


def test_lab_discover_lists_unplaced_and_runs_no_ladder(tmp_path):
    ctx, calls = _lab_ctx(tmp_path)
    run_discover(ctx)
    u = ctx.db.unplaced()
    assert [(e["port"], e["lane"], e["serial"], e["bmc_ip"]) for e in u] == [(18, 1, "QTFCKQ024030B", "172.31.0.54")]
    lr = ctx.db.latest_runs()
    assert ("C18", "switchportlink") not in lr and ("BB20", "switchportlink") not in lr
    assert lr[("_rack", "unplaced")]["detail"]["headline"] == "1 unplaced device(s)"
    assert not [c for c in calls if c[0] in ("power_action", "set_boot_pxe")]


def test_lab_discover_enclosure_steps_are_na_via_head(tmp_path):
    ctx, calls = _lab_ctx(tmp_path)
    ctx.db.place("C18", serial="QTFCKQ024030B", product="Tioga Pass Single Side", family="tiogapass",
                 switch="H", port=18, lane=1, host_mac="0c42a1ecbc0e", bmc_mac="0c42a1ecbc11", who="op")
    run_discover(ctx, positions=["BB20"])
    r = ctx.db.latest_runs()[("BB20", "switchportlink")]
    assert r["verdict"] == "skip" and r["detail"]["na"] is True
    assert "judged through its head C18" in r["detail"]["headline"]


def test_probe_tries_the_bmc_side_first_and_stops():
    calls = []

    def probe(ip):
        calls.append(ip)
        return TP_FRU if ip == "172.31.0.54" else None
    by_port = {(18, 1): ["0c42a1ecbc0e", "0c42a1ecbc11"]}
    leases = {"0c42a1ecbc0e": "172.31.0.58", "0c42a1ecbc11": "172.31.0.54"}
    out = find(by_port, {}, leases, probe, uplinks=set())
    assert calls == ["172.31.0.54"] and out[0]["bmc_mac"] == "0c42a1ecbc11"
    assert out[0]["macs"] == ["0c42a1ecbc0e", "0c42a1ecbc11"]       # listed in port order


def test_wedge400_is_a_bmc_plus_cpu_switch():
    from flax_rack.geometry import parse_geometry, SWITCH_ENDPOINTS
    assert SWITCH_ENDPOINTS["wedge400"] == {"bmc": 1, "cpu": 1}
    g = parse_geometry(base(lab=True, devices=[dev("L31", "compute-tp")],
                            switches=[{"id": "H", "role": "mgmt", "ou": 19, "model": "arista", "nos": "eos",
                                       "mgmt_ip": "192.168.88.34", "uplink_ports": [32]},
                                      {"id": "W", "role": "spare", "ou": 28, "model": "wedge400", "nos": "sonic",
                                       "site_port": {"switch": "turtle-rhythm", "port": "swp21"}}]))
    assert g.switches[1].endpoints == {"bmc": 1, "cpu": 1}


def test_only_switches_rackpost_reads_need_logins():
    from flax_rack.geometry import parse_geometry
    from flax_rack.server import switch_noses
    g = parse_geometry(base(lab=True, devices=[dev("L31", "compute-tp")],
                            switches=[{"id": "H", "role": "mgmt", "ou": 19, "model": "arista", "nos": "eos",
                                       "mgmt_ip": "192.168.88.34", "uplink_ports": [32]},
                                      {"id": "W", "role": "spare", "ou": 28, "model": "wedge400", "nos": "sonic",
                                       "site_port": {"switch": "turtle-rhythm", "port": "swp21"}}]))
    assert switch_noses(g) == {"eos"}


def test_wedge400_ladder_reads_the_bmc_and_identifies_the_cpu(tmp_path):
    from flax_rack.creds import Creds
    bmc, cpu = "34ad610013d3", "c018507a8972"
    g = parse_geometry(base(lab=True, devices=[dev("L31", "compute-tp")],
                            switches=[{"id": "H", "role": "mgmt", "ou": 19, "model": "arista", "nos": "eos",
                                       "mgmt_ip": "192.168.88.34", "uplink_ports": [32]},
                                      {"id": "W", "role": "spare", "ou": 28, "model": "wedge400", "nos": "sonic",
                                       "site_port": {"switch": "turtle-rhythm", "port": "swp21"}}]))
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    calls = []

    def wedge_bmc(ip, u, p, syseeprom=None, runner=None):
        calls.append(("wedge_bmc", ip))
        if ip == "192.168.88.234":
            return RungResult("pass", {"local_mac": "34:ad:61:00:13:d3", "serial": "W400SN1",
                                       "headline": "SN W400SN1"}, {"weutil.txt": "x"})
        return RungResult("unmeasured", {"error": "no answer"}, {})
    R = SimpleNamespace(
        read_fdb=lambda *a, **k: RungResult("pass", {"fdb": [], "count": 0}),
        read_ifstatus=lambda *a, **k: RungResult("pass", {"status": {}}),
        site_switches=lambda: {"turtle-rhythm": {"host": "192.168.88.33", "driver": "cumulus"}},
        mac_table=lambda *a, **k: RungResult("pass", {"ports": {"swp21": [cpu, bmc]}}),
        wedge_bmc=wedge_bmc, ping=lambda ip, runner=None: True, tp_fru=lambda *a, **k: RungResult("unmeasured", {}, {}),
        host_run=lambda *a, **k: calls.append(("host_run",) + a) or (0, "", ""),
        read_syseeprom=lambda *a, **k: calls.append(("syseeprom",) + a) or RungResult("fail", {"error": "x"}),
        wall=lambda: 0, clock=lambda: 0, sleep=lambda s: None)
    kea = FakeKea()
    kea.dyn.update({bmc: "192.168.88.234", cpu: "192.168.88.232"})
    cr = Creds({"eosuser": "e", "eospass": "e", "cumuser": "c", "cumpass": "c"},
               [("bu", "bp")], ("flax", "hp"), [("ru", "rp")], ("ou", "op"))
    ctx = OrderContext(tmp_path, g, db, kea, cr, fake_runner, R)
    run_discover(ctx, positions=["_sw-W"])
    lr = db.latest_runs()
    v = lambda s: (lr.get(("_sw-W", s)) or {}).get("verdict")
    assert v("switchportlink") == "pass" and v("bmc-mac-seen") == "pass" and v("serial") == "pass"
    assert v("host-mac-seen") == "pass" and v("host-pinged") == "pass" and v("bmc-ready") == "pass"
    assert v("host-ssh") == "skip" and lr[("_sw-W", "host-ssh")]["detail"]["na"] is True
    assert not [c for c in calls if c[0] in ("host_run", "syseeprom")]      # the CPU is never logged in to


def test_wedge400_cpu_module_fills_the_mgmt_row():
    from flax_rack import devview
    from flax_rack.geometry import parse_geometry
    g = parse_geometry(base(lab=True, devices=[dev("L31", "compute-tp")],
                            switches=[{"id": "W", "role": "spare", "ou": 28, "model": "wedge400", "nos": "sonic",
                                       "site_port": {"switch": "turtle-rhythm", "port": "swp21"}}]))
    runs = {"host-mac-seen": {"verdict": "pass", "detail": {"mac": "c018507a8972"}},
            "host-leased": {"verdict": "pass", "detail": {"ip": "172.31.0.52"}},
            "bmc-mac-seen": {"verdict": "pass", "detail": {"mac": "34ad610013d3"}},
            "bmc-leased": {"verdict": "pass", "detail": {"ip": "172.31.0.50"}}}
    v = devview.switch_view(g, g.switches[0], runs, {})
    assert v["sonic"]["ip"] == "172.31.0.52" and v["bmc"]["ip"] == "172.31.0.50"
