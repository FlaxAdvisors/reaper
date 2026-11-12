import pytest
pytest.importorskip("flax_rack.iperf_mesh_view")
from flax_rack import iperf_mesh_view as v
from flax_rack.geometry import load_geometry
from tests.test_flax_rack_geometry import FIX
from tests.test_flax_rack_qualify import profiles  # noqa: F401 -- fixture

# 2026-11-04 RRORD-101 R05 (100G heads), measured against the iperf server L05
R05 = {"A": {"tx_bps": 93620663008.37573, "rx_bps": 94127229984.16682, "speed_bps": 1e11, "ok": True},
       "B": {"tx_bps": 94127866874.15175, "rx_bps": 94123314373.9326, "speed_bps": 1e11, "ok": True},
       "mgmt": {"tx_bps": 941200241.238968, "rx_bps": 941200319.5638893, "speed_bps": 1e9, "ok": True}}


def node(gbps, speed):
    return {"tx_bps": gbps * 1e9, "rx_bps": gbps * 1e9, "speed_bps": speed,
            "ok": gbps * 1e9 >= 0.8 * speed}


def compute(a=23.5):
    return {"mgmt": node(0.94, 1e9), "A": node(a, 25e9), "B": node(23.5, 25e9)}


def test_client_view_table_and_headline():
    out = v.client_view(R05, {"mgmt": "C:43", "A": "A:28/2", "B": "B:28/2"})
    assert out["headline"] == "A 93.6/94.1 · B 94.1/94.1 · mgmt 0.94/0.94 Gb/s · min 94% of line"
    t = out["table"]
    assert t["cols"] == ["network", "port", "link", "TX Gb/s", "RX Gb/s", "% of line", "judge"]
    assert t["rows"] == [["mgmt", "C:43", "1G", "0.94", "0.94", "94%", "ok"],
                         ["A", "A:28/2", "100G", "93.6", "94.1", "94%", "ok"],
                         ["B", "B:28/2", "100G", "94.1", "94.1", "94%", "ok"]]
    assert t["row_cls"] == ["", "", ""]
    assert any("0.8" in n for n in out["notes"])
    assert out["A"] == R05["A"]                         # the raw fields stay


def test_server_view_rack_table():
    clients = {"R05": compute(), "C37": compute(), "L37": compute(), "R37": compute(a=9.0)}
    ports = {p: {"mgmt": "C:7", "A": "A:8/4", "B": "B:8/4"} for p in clients}
    order = ["L37", "C37", "R37", "L05", "R05"]
    out = v.server_view({"served": ["R05", "C37", "L37", "R37"], "server_pids": {}},
                        clients, ports, order)
    t = out["table"]
    assert len(t["rows"]) == 12
    assert [(r[0], r[1]) for r in t["rows"]] == [
        (p, n) for p in ("L37", "C37", "R37", "R05") for n in ("mgmt", "A", "B")]
    assert t["cols"][0] == "client" and t["rows"][0][2] == "C:7"
    assert out["headline"] == "served 4 nodes · 12 paths · min 36% of line · worst: R37 A 36%"
    assert t["row_cls"][7] == "bad" and t["rows"][7][-1] == "below 80%"
    assert out["served"] == ["R05", "C37", "L37", "R37"]
    ok = v.server_view({"served": ["L37"]}, {"L37": compute()}, {}, order)
    assert ok["headline"] == "served 1 node · 3 paths · min 94% of line"


def test_mesh_views_with_missing_network_result():
    det = compute()
    det["B"] = {"tx_bps": None, "rx_bps": None, "speed_bps": 25e9, "ok": False}
    det["error"] = "B: tx None / rx None Mbit/s < 20000 (0.8 x 25000)"
    out = v.client_view(det, {})
    b = out["table"]["rows"][2]
    assert b[0] == "B" and b[3:] == ["—", "—", "—", "no result"]
    assert out["table"]["row_cls"][2] == "bad"
    assert out["headline"] == "A 23.5/23.5 · B no result · mgmt 0.94/0.94 Gb/s · min 94% of line"
    # the rack view keeps that client, and one the server never measured at all
    srv = v.server_view({"served": ["L37", "C37"]},
                        {"L37": det, "C37": {"error": "client has no networks"}}, {},
                        ["L37", "C37"])
    rows = srv["table"]["rows"]
    assert [r[0] for r in rows] == ["L37"] * 3 + ["C37"]
    assert rows[2][-1] == "no result" and rows[3][-1] == "no result: client has no networks"
    assert srv["headline"] == "served 2 nodes · 3 paths · min 94% of line · 2 no result"
    # a detail with no network (server not ready) is left as it was
    nr = {"error": "server L05 not ready: x", "headline": "server L05 not ready: x"}
    assert v.client_view(nr, {}) == nr


def test_port_column_prefers_lldp_seen():
    g = load_geometry(FIX)
    d = g.device("R05")                     # pcie0 A:28/2, pcie1 B:28/2, mezz0 C:43
    assert v.ports_of(g, d, None) == {"mgmt": "C:43", "A": "A:28/2", "B": "B:28/2",
                                      "bond": "A:28/2 + B:28/2"}
    ha = {"links": [
        {"link": "pcie0", "expected": "A Ethernet109 (28/2)", "seen": "A Ethernet112",
         "ifname": "enp1s0f0"},
        {"link": "pcie1", "expected": "B Ethernet109 (28/2)", "seen": "B Ethernet109",
         "ifname": "enp1s0f1"}]}
    p = v.ports_of(g, d, ha)
    assert p["A"] == "planned A:28/2, seen A:29/1" and p["B"] == "B:28/2" and p["mgmt"] == "C:43"
    # a link LLDP did not see falls back to the plan
    ha["links"][0].update(seen=None, ifname=None)
    assert v.ports_of(g, d, ha)["A"] == "A:28/2"


def test_qualify_records_mesh_views_and_artifacts(tmp_path, profiles):
    from tests.test_flax_rack_qualify import make_ctx
    from flax_rack.qualify import run_qualify
    ctx = make_ctx(tmp_path)
    run_qualify(ctx)
    lr = ctx.db.latest_runs()
    c = lr[("R05", "iperf-mesh")]
    assert c["verdict"] == "pass"
    assert [r[:2] for r in c["detail"]["table"]["rows"]] == [["mgmt", "C:43"], ["A", "A:28/2"],
                                                             ["B", "B:28/2"]]
    assert c["detail"]["headline"].endswith("min 94% of line")
    assert sorted(c["artifacts"]) == sorted(f"iperf-{n}-{d}" for n in ("mgmt", "A", "B")
                                            for d in ("tx", "rx"))
    assert (ctx.dir / c["artifacts"]["iperf-A-tx"]).read_text().startswith("{")
    s = lr[("L05", "iperf-mesh")]
    assert s["verdict"] == "pass" and s["detail"]["served"] == ["L37", "C37", "R37", "R05"]
    assert [r[0] for r in s["detail"]["table"]["rows"][::3]] == ["L37", "C37", "R37", "R05"]
    assert s["detail"]["headline"] == "served 4 nodes · 12 paths · min 94% of line"
