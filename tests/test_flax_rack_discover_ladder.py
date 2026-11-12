"""rackpost Discover = loose-node Post's 16-step ladder per device (plan 2b Task 4)."""
import datetime
import json
import os
import time

import pytest

pytest.importorskip("flax_rack.discover_ladder")
from flax_rack import discover_ladder as dl
from flax_rack.creds import load_creds
from flax_rack.fakes import FakeRungs, with_site_ports, with_wedge_ips
from flax_rack.geometry import load_geometry, parse_geometry
from flax_rack.orderdb import OrderDB
from flax_rack.qualify import qualify_passed, run_qualify
from flax_rack.stages import OrderContext, run_discover
from tests.test_flax_rack_geometry import FIX
from tests.test_flax_rack_qualify import det, make_ctx, profiles, v  # noqa: F401
from tests.test_flax_rack_stages import CREDS, FakeKea, fake_runner

HOSTS = ("L37", "C37", "R37", "L05", "R05")


def make(tmp_path, *, live=(), site_ports=True, **rk):
    g = load_geometry(FIX)
    g = with_site_ports(g) if site_ports else g
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    R = FakeRungs(g, live=live, **rk)
    kea = FakeKea()
    kea.dyn.update(R.switch_leases())
    return OrderContext(tmp_path, g, db, kea, CREDS, fake_runner, R)


def runs(ctx, key):
    """[(rung, verdict)] of `key`'s Discover rows, in recording order."""
    import sqlite3
    c = sqlite3.connect(ctx.db.path)
    rows = c.execute("SELECT rung, verdict FROM run WHERE pos=? AND stage='discover' ORDER BY id",
                     (key,)).fetchall()
    c.close()
    return rows


def calls(ctx, name, pos=None):
    return [c for c in ctx.rungs.calls if c[0] == name and (pos is None or c[1] == pos)]


# ---------------------------------------------------------------- parity with Post
def test_steps_budgets_and_proof_are_posts():
    from flax_post import blades
    from flax_post.observe import ladder
    assert dl.DISCOVER_STEPS == blades.DISCOVER_STEPS
    for k, val in dl.LADDER_BUDGET_S.items():
        assert blades.LADDER_BUDGET_S[k] == val, k
    assert dl.BMC_READY_RETRY_BUDGETS_S == blades.BMC_READY_RETRY_BUDGETS_S
    assert dl.BOOT_PROOF_BOUND_S == ladder.BOOT_PROOF_BOUND_S
    assert set(dl.HOST_STEPS) | {"switchportlink", "bmc-mac-seen", "bmc-reserved", "bmc-leased",
                                 "bmc-pinged", "serial", "bmc-ready"} == set(dl.DISCOVER_STEPS)
    born, k = 1000.0, 1200.0
    cases = [
        {"tftp": [1100], "ipxe": [1170], "iso": [1240]},            # proven
        {"tftp": [1100], "ipxe": [1170], "iso": [1400]},            # ISO too late
        {"tftp": [900], "ipxe": [1170], "iso": [1240]},             # tftp before birth
        {"tftp": [1000], "ipxe": [1130], "iso": [1250]},            # tftp->ipxe 130 > 120
        {"ipxe": [990], "iso": [1100]},                             # before birth
        {"tftp": [1100], "ipxe": [1170, 1210], "iso": [1240]},      # newest iPXE after kernel
    ]
    for t in cases:
        for kb in (k, None, 1300.0):
            assert dl.prove_boot(born, t, kb) == ladder._prove_boot(born, t, kb), (t, kb)
    assert dl.prove_boot(born, cases[0], k) == {"tftp": 1100, "ipxe": 1170, "iso": 1240,
                                                "kernel": k}


# ---------------------------------------------------------------- full ladder
def test_full_ladder_happy_path(tmp_path):
    ctx = make(tmp_path)
    run_discover(ctx)
    lr = ctx.db.latest_runs()
    for pos in HOSTS:
        assert runs(ctx, pos) == [(s, "pass") for s in dl.DISCOVER_STEPS], pos
        for s in dl.DISCOVER_STEPS:
            d = lr[(pos, s)]["detail"]
            assert d["headline"] and isinstance(d["rows"], list) and isinstance(d["notes"], list)
            assert all(len(r) == 2 for r in d["rows"]), (pos, s)
        assert ("power_action", pos, "on") in ctx.rungs.calls
    # evidence rows (Post's stepinfo labels)
    labels = lambda pos, s: [r[0] for r in lr[(pos, s)]["detail"]["rows"]]
    assert {"seen at", "after power-on", "evidence", "budget"} <= set(labels("L37", "tftp-seen"))
    assert lr[("L37", "tftp-seen")]["detail"]["rows"][1] == ["after power-on", "+20s"]
    assert {"BMC IP", "ping", "chassis power"} <= set(labels("L37", "bmc-pinged"))
    assert {"BMC MAC", "reservation IP"} <= set(labels("L37", "bmc-reserved"))
    assert lr[("L37", "serial")]["detail"]["serial"] == "FAKE371"
    assert ["serial (Product Serial)", "FAKE371"] in lr[("L37", "serial")]["detail"]["rows"]
    rel = lr[("L37", "serial")]["artifacts"]["fru0"]
    assert rel.startswith("artifacts/L37/d") and (ctx.dir / rel).read_text().startswith("FRU Device")
    hs = lr[("L37", "host-ssh")]["detail"]
    assert hs["boot_id"] and hs["bootif"] == "02:fa:25:01:00:10" and hs["already_live"] is False
    # a powered-off node shows only its BMC: host derived, seen after the boot
    assert lr[("L37", "bmc-mac-seen")]["detail"]["note"] == "host derived (host MAC not seen)"
    assert lr[("L37", "host-mac-seen")]["detail"]["headline"].endswith("seen after the boot")
    row = {x["pos"]: x for x in ctx.db.devices()}["L37"]
    assert row["host_mac"] == "02fa25010010" and row["bmc_mac"] == "02fa25010012"
    ev = ctx.db.events()[0]["text"]
    assert ev == "discover done; 1 unexpected MAC(s); every device passed"


def test_parallel_at_most_eight(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(dl, "MAX_PARALLEL", 2)
    ctx = make(tmp_path)
    live, peak, mu = [0], [0], threading.Lock()
    base = ctx.rungs.read_ifstatus
    base_tp = ctx.rungs.tp_fru

    def tp(*a, **k):
        with mu:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.02)
        with mu:
            live[0] -= 1
        return base_tp(*a, **k)
    ctx.rungs.tp_fru = tp
    run_discover(ctx)
    assert peak[0] == 2 and dl.MAX_PARALLEL == 2 and base


def test_failed_step_skips_the_rest(tmp_path):
    ctx = make(tmp_path)
    ctx.rungs.down.add("172.32.37.11")              # L37's BMC never answers
    run_discover(ctx)
    got = dict(runs(ctx, "L37"))
    assert got["bmc-pinged"] == "fail"
    assert all(got[s] == "skip" for s in dl.DISCOVER_STEPS[5:])
    assert det(ctx, "L37", "power-on")["reason"] == "bmc-pinged failed"
    assert not calls(ctx, "power_action", "L37")


def test_serial_fail_does_not_stop_the_walk(tmp_path):
    ctx = make(tmp_path)
    base = ctx.rungs.tp_fru

    def tp(ip, creds, **k):
        r = base(ip, creds, **k)
        if ctx.rungs.ip_role[ip][0] == "R37" and not calls(ctx, "power_action", "R37"):
            from flax_rungs.identity import _tp_result
            return _tp_result("FRU Device Description : Builtin FRU Device (ID 0)\n"
                              " Product Name : Tioga Pass\n Product Serial :\n", "u", 17)
        return r
    ctx.rungs.tp_fru = tp
    run_discover(ctx)
    assert v(ctx, "R37", "serial") == "fail"
    assert det(ctx, "R37", "serial")["error"] == "FRU 0 no ship serial"
    assert v(ctx, "R37", "bmc-ready") == "pass"


def test_tftp_budget_expires_on_the_virtual_clock(tmp_path):
    ctx = make(tmp_path)
    ctx.rungs.no_marks.add("C37")
    t0 = ctx.rungs.t
    run_discover(ctx)
    assert v(ctx, "C37", "tftp-seen") == "fail"
    assert det(ctx, "C37", "tftp-seen")["error"] == "no tftp after 600s"
    assert v(ctx, "C37", "host-ssh") == "skip" and ctx.rungs.t - t0 >= 600


def test_power_on_cycles_a_node_running_another_os(tmp_path):
    ctx = make(tmp_path)
    ctx.rungs.power["R05"] = "on"                    # on, no live image answering ssh
    run_discover(ctx)
    assert ("power_action", "R05", "cycle") in ctx.rungs.calls
    assert det(ctx, "R05", "power-on")["power_action"] == "cycle"
    assert v(ctx, "R05", "host-ssh") == "pass"


def test_power_unknown_never_acts(tmp_path):
    ctx = make(tmp_path)
    ctx.rungs.power["R05"] = None
    run_discover(ctx)
    assert det(ctx, "R05", "power-on")["error"] == "power state unknown (no IPMI answer); not acting"
    assert not calls(ctx, "power_action", "R05") and not calls(ctx, "set_boot_pxe", "R05")


# ---------------------------------------------------------------- already live
def test_already_live_node_no_power_action_markers_proven_at_ssh(tmp_path):
    ctx = make(tmp_path, live={"L37"})
    run_discover(ctx)
    assert not calls(ctx, "power_action", "L37") and not calls(ctx, "set_boot_pxe", "L37")
    po = det(ctx, "L37", "power-on")
    assert v(ctx, "L37", "power-on") == "pass" and po["already_live"] is True
    assert po["headline"] == "already in the live image: no power action"
    b = ctx.rungs.boots["L37"]
    assert po["kernel_boot"] == pytest.approx(b["kernel"])
    for s, k in (("tftp-seen", "tftp"), ("ipxe-seen", "ipxe"), ("live-iso-seen", "iso")):
        d = det(ctx, "L37", s)
        assert v(ctx, "L37", s) == "pass" and d["proven_at_ssh"] is True, s
        assert d["seen_at"] == b[k] and d["headline"].startswith("proven at ssh")
        assert "proven at ssh" in d["notes"][0]
    hs = det(ctx, "L37", "host-ssh")
    assert v(ctx, "L37", "host-ssh") == "pass" and hs["boot_id"] == b["boot_id"]
    assert hs["already_live"] is True
    assert runs(ctx, "L37") == [(s, "pass") for s in dl.DISCOVER_STEPS]
    assert ("power_action", "C37", "on") in ctx.rungs.calls        # the others boot


def test_already_live_markers_not_in_logs_skip_never_cycle(tmp_path):
    ctx = make(tmp_path, live={"L37"})
    ctx.rungs.no_marks.add("L37")
    run_discover(ctx)
    assert not calls(ctx, "power_action", "L37")
    for s in ("tftp-seen", "ipxe-seen", "live-iso-seen"):
        assert v(ctx, "L37", s) == "skip"
        assert det(ctx, "L37", s)["reason"].startswith("already in the live image")
    assert v(ctx, "L37", "host-ssh") == "pass"


def test_live_image_of_another_host_fails_without_acting(tmp_path):
    ctx = make(tmp_path, live={"L37"}, wrong_host={"L37"})
    run_discover(ctx)
    assert det(ctx, "L37", "power-on")["error"].startswith("wrong host at 172.32.37.1: BOOTIF 02:fa:ff:ff:00:99")
    assert not calls(ctx, "power_action", "L37") and not calls(ctx, "set_boot_pxe", "L37")
    assert v(ctx, "L37", "tftp-seen") == "skip"


def test_on_without_host_creds_is_not_cycled(tmp_path):
    import dataclasses
    ctx = make(tmp_path, live={"L37"})
    ctx.creds = dataclasses.replace(CREDS, host=())
    run_discover(ctx)
    assert det(ctx, "L37", "power-on")["error"].startswith("power is on and the live image cannot be checked")
    assert not calls(ctx, "power_action", "L37")


# ---------------------------------------------------------------- boot logs
def _dnsmasq_line(ts, ip):
    return time.strftime("%b %d %H:%M:%S", time.localtime(ts)) + \
        f" dnsmasq-tftp[6960]: sent /srv/tftpboot/efi64/ipxe.efi to {ip}\n"


def test_tftp_only_in_rotated_log_is_seen(tmp_path):
    now = time.time()
    since, ts = now - 120, now - 60
    dn = tmp_path / "dnsmasq.log"
    dn.write_text(_dnsmasq_line(now - 30, "172.32.9.9"))            # after the rotation
    day = datetime.date.fromtimestamp(ts).strftime("%Y%m%d")
    (tmp_path / f"dnsmasq.log-{day}").write_text(_dnsmasq_line(ts, "172.32.37.1"))
    (tmp_path / f"dnsmasq.log-{day}.gz").write_bytes(b"\x1f\x8b not read")
    old = (datetime.date.fromtimestamp(since) - datetime.timedelta(days=3)).strftime("%Y%m%d")
    (tmp_path / f"dnsmasq.log-{old}").write_text(_dnsmasq_line(ts, "172.32.37.1"))
    ng = tmp_path / "access.log"
    ng.write_text("")
    assert dl.rotated_dnsmasq(str(dn), since) == [str(tmp_path / f"dnsmasq.log-{day}")]
    m = dl.boot_marks("172.32.37.1", since, dnsmasq=str(dn), nginx=str(ng))
    assert m["tftp"] == [pytest.approx(int(ts), abs=1)] and m["rotated_tftp"] == m["tftp"]
    assert m["sources"]["tftp"] == [str(dn), str(tmp_path / f"dnsmasq.log-{day}")]
    # and the ladder step reports it, noting the rotation
    ctx_env = dl.Env(type("R", (), {"boot_marks": staticmethod(
        lambda ip, s: dl.boot_marks(ip, s, dnsmasq=str(dn), nginx=str(ng))),
        "clock": staticmethod(time.monotonic), "wall": staticmethod(time.time),
        "sleep": staticmethod(lambda s: None)}), CREDS)
    st = dl._Boot(power_at=since)
    r = dl._marker(None, ctx_env, st, "172.32.37.1", "tftp-seen", "tftp")
    assert r.verdict == "pass"
    assert any("found only in a rotated dnsmasq log" in n for n in r.detail["notes"])


def test_boot_marks_reads_posts_line_formats(tmp_path):
    now = time.time()
    dn, ng = tmp_path / "dnsmasq.log", tmp_path / "access.log"
    dn.write_text(_dnsmasq_line(now - 200, "172.32.37.1"))
    stamp = lambda t: time.strftime("%d/%b/%Y:%H:%M:%S +0000", time.gmtime(t))
    ng.write_text(
        f'172.32.37.1 - - [{stamp(now - 130)}] "GET /v32/boot.ipxe HTTP/1.1" 200 24091 "-" "iPXE/1.21.1+ (gc1834)"\n'
        f'172.32.37.1 - - [{stamp(now - 60)}] "GET /install//suse/live/test/LiveLeap.x86_64-3.0.0.iso HTTP/1.1" 200 9 "-" "curl/8.12.1"\n')
    m = dl.boot_marks("172.32.37.1", now - 300, dnsmasq=str(dn), nginx=str(ng))
    assert [len(m[k]) for k in ("tftp", "ipxe", "iso")] == [1, 1, 1]
    assert m["ipxe"][0] == pytest.approx(now - 130, abs=1)


# ---------------------------------------------------------------- switches
def test_switch_devices(tmp_path):
    ctx = make(tmp_path)
    run_discover(ctx)
    lr = ctx.db.latest_runs()
    for sw in ("A", "B"):                                # wedges: SONiC + BMC
        key = f"_sw-{sw}"
        got = dict(runs(ctx, key))
        assert [r for r, _ in runs(ctx, key)] == list(dl.DISCOVER_STEPS)
        for s in ("switchportlink", "bmc-mac-seen", "bmc-leased", "bmc-pinged", "serial",
                  "host-mac-seen", "host-leased", "host-pinged", "host-ssh", "bmc-ready"):
            assert got[s] == "pass", (key, s, lr[(key, s)]["detail"])
        for s in ("bmc-reserved", "host-reserved"):
            assert lr[(key, s)]["detail"]["reason"].startswith("switch endpoints are discovered")
        for s in ("power-on", "tftp-seen", "ipxe-seen", "live-iso-seen"):
            assert lr[(key, s)]["detail"]["reason"] == "a switch has no PXE live boot"
        m = ctx.rungs.sw_macs[sw]
        assert lr[(key, "bmc-mac-seen")]["detail"]["mac"] == m["bmc"]
        assert lr[(key, "host-mac-seen")]["detail"]["mac"] == m["sonic"]
        assert lr[(key, "bmc-leased")]["detail"]["ip"] == ctx.kea.dyn[m["bmc"]]
        assert lr[(key, "serial")]["detail"]["serial"] == f"AIFAKE{sw}"
        assert ["SONiC syseeprom serial", f"AIFAKE{sw} (matches)"] in lr[(key, "serial")]["detail"]["rows"]
    got = dict(runs(ctx, "_sw-C"))                       # Dell: SONiC only
    assert got["serial"] == got["host-ssh"] == got["host-leased"] == "pass"
    assert lr[("_sw-C", "serial")]["detail"]["serial"] == "CNFAKE0C"
    for s in ("bmc-mac-seen", "bmc-leased", "bmc-ready"):
        assert got[s] == "skip" and "no BMC" in lr[("_sw-C", s)]["detail"]["reason"]
    # discovered, never reserved; the turtle read through site_port + cisco creds
    sw_macs = {m for x in ctx.rungs.sw_macs.values() for m in x.values()}
    assert not [u for u in ctx.kea.upserts if u["mac"] in sw_macs]
    assert ("mac_table", "fake-turtle", ("Gi1/0/18",)) in ctx.rungs.calls


def test_switch_without_site_port(tmp_path):
    ctx = make(tmp_path, site_ports=False)
    run_discover(ctx)
    d = det(ctx, "_sw-A", "switchportlink")
    assert d["error"] == "switch A has no site_port in geometry.json"
    assert dict(runs(ctx, "_sw-A"))["host-ssh"] == "skip"


def test_switch_without_cisco_creds(tmp_path):
    import dataclasses
    ctx = make(tmp_path)
    ctx.creds = dataclasses.replace(CREDS, switch={"sonicuser": "u", "sonicpass": "p"})
    run_discover(ctx)
    assert det(ctx, "_sw-C", "switchportlink")["error"] == \
        "add cisco_user/cisco_pass to the vaulted credentials.json"


def test_wedge_bmc_told_from_sonic_by_weutil(tmp_path):
    ctx = make(tmp_path)
    m = ctx.rungs.sw_macs["A"]
    ctx.rungs.sw_macs["A"] = {"bmc": m["bmc"], "sonic": m["sonic"]}   # order does not matter
    run_discover(ctx)
    assert det(ctx, "_sw-A", "bmc-mac-seen")["mac"] == m["bmc"]
    assert det(ctx, "_sw-A", "host-mac-seen")["mac"] == m["sonic"]


def test_site_switches_reads_switches_json(tmp_path):
    p = tmp_path / "switches.json"
    p.write_text(json.dumps([{"name": "turtle-gouda", "driver": "ios", "host": "192.0.2.33"},
                             {"name": "x", "driver": "eos"}]))
    assert dl.site_switches(str(p)) == {
        "turtle-gouda": {"name": "turtle-gouda", "driver": "ios", "host": "192.0.2.33"}}


def test_geometry_site_port_and_switch_endpoints():
    from flax_rack.geometry import GeometryError
    d = json.loads(FIX.read_text())
    d["switches"][0]["site_port"] = {"switch": "turtle-gouda", "port": "Gi1/0/19"}
    g = parse_geometry(d)
    assert g.switches[0].site_port == {"switch": "turtle-gouda", "port": "Gi1/0/19"}
    assert g.switches[0].endpoints == {"sonic": 1}
    assert g.switches[1].endpoints == {"sonic": 1, "bmc": 1} and g.switches[1].site_port is None
    d["switches"][0]["site_port"] = {"switch": "t", "port": "Gi1/0/19; reload"}
    with pytest.raises(GeometryError, match="bad site_port"):
        parse_geometry(d)


def test_creds_cisco_and_obmc_optional(tmp_path):
    m, b = tmp_path / "c.json", tmp_path / "b.json"
    m.write_text(json.dumps({"sonicuser": "u", "sonicpass": "p", "cisco_user": "c",
                             "cisco_pass": "cp", "obmcuser": "o", "obmcpass": "op"}))
    b.write_text("[]")
    c = load_creds(str(m), str(b), str(tmp_path / "none"), str(tmp_path / "none"))
    assert c.has_switch("ios") and c.switch["cisco_pass"] == "cp" and c.obmc == ("o", "op")
    m.write_text(json.dumps({"sonicuser": "u", "sonicpass": "p"}))
    c = load_creds(str(m), str(b), str(tmp_path / "none"), str(tmp_path / "none"))
    assert not c.has_switch("ios") and c.obmc == ()


# ---------------------------------------------------------------- JBOD
def test_jbod_iom_endpoints(tmp_path):
    ctx = make(tmp_path)
    run_discover(ctx)
    lr = ctx.db.latest_runs()
    for n in (1, 2):
        key = f"JBOD01.iom{n}"
        got = runs(ctx, key)
        assert [r for r, _ in got] == list(dl.DISCOVER_STEPS)
        g = dict(got)
        for s in ("switchportlink", "bmc-mac-seen", "bmc-reserved", "bmc-leased", "bmc-pinged",
                  "serial", "bmc-ready"):
            assert g[s] == "pass", (key, s)
        for s in dl.HOST_STEPS:
            assert g[s] == "skip" and lr[(key, s)]["detail"]["reason"] == "an IOM has no host"
        assert lr[(key, "serial")]["detail"]["serial"] == f"J49FAKE{n}"
        assert lr[(key, "serial")]["artifacts"]["fruid-util-iom"].startswith(f"artifacts/{key}/d")
    # the device's own rows roll the IOMs up
    assert dict(runs(ctx, "JBOD01"))["serial"] == "pass"
    assert lr[("JBOD01", "power-on")]["detail"]["headline"] == "an IOM has no host"
    assert lr[("JBOD01", "bmc-mac-seen")]["detail"]["iom_macs"] == ["02fa01000021", "02fa01000022"]
    assert {r["role"] for r in lr[("JBOD01", "bmc-reserved")]["detail"]["reserved"]} == {"iom1", "iom2"}
    assert {d["pos"]: d for d in ctx.db.devices()}["JBOD01"]["iom_macs"] == \
        ["02fa01000021", "02fa01000022"]
    assert {u["hostname"] for u in ctx.kea.upserts} >= {"rr101-jbod01-iom1", "rr101-jbod01-iom2"}


def test_jbod_one_iom_down_faults_device(tmp_path):
    ctx = make(tmp_path)
    ctx.rungs.down.add("172.32.1.12")
    run_discover(ctx)
    assert v(ctx, "JBOD01.iom2", "bmc-pinged") == "fail"
    assert v(ctx, "JBOD01.iom1", "bmc-ready") == "pass"
    assert v(ctx, "JBOD01", "bmc-pinged") == "fail"
    assert det(ctx, "JBOD01", "bmc-pinged")["error"].startswith("iom2: no ping from 172.32.1.12")
    assert "JBOD01" in qualify_passed(ctx.db, ctx.g)[1]


def test_jbod_iom_side_against_geometry_and_link_wrong(tmp_path):
    ctx = make(tmp_path)
    assert ctx.g.device("JBOD01").sides == {"iom1": "A", "iom2": "B"}    # bc-jbod default
    run_discover(ctx)
    assert det(ctx, "JBOD01.iom1", "serial")["side"] == "A"
    assert v(ctx, "JBOD01", "serial") == "pass"
    # the geometry says the cables are the other way round: both IOMs read `wrong`
    ctx.g.device("JBOD01").sides.update(iom1="B", iom2="A")
    run_discover(ctx, positions=["JBOD01"])
    d1 = det(ctx, "JBOD01.iom1", "serial")
    assert v(ctx, "JBOD01.iom1", "serial") == "fail" and d1["status"] == "wrong"
    assert d1["headline"] == "wrong IOM: Side A, want Side B" and d1["iom_mac"] == "02fa01000021"
    assert v(ctx, "JBOD01", "serial") == "fail"
    # ... and the link monitor flags the iom1/iom2 ports (MAC -> side from those reads)
    assert ctx.db.iom_sides() == {"02fa01000021": "A", "02fa01000022": "B"}
    from flax_rack.linkmon import device_macs, device_names, evaluate
    c = {"at": 1.0, "ifaces": {f"Ethernet{i}": {"oper": "up"} for i in (12, 13)}, "lldp": [],
         "fdb": [{"mac": "02:fa:01:00:00:21", "iface": "Ethernet12"},
                 {"mac": "02:fa:01:00:00:22", "iface": "Ethernet13"}]}
    r = evaluate(ctx.g, {"C": c}, device_macs(ctx.db), device_names(ctx.g), 1.0,
                 ctx.db.iom_sides())["JBOD01"]
    assert r["iom1"]["state"] == "wrong"
    assert r["iom1"]["text"] == "sees 02:fa:01:00:00:21 (Side A IOM, want Side B)"
    ctx.g.device("JBOD01").sides.update(iom1="A", iom2="B")
    r = evaluate(ctx.g, {"C": c}, device_macs(ctx.db), device_names(ctx.g), 1.0,
                 ctx.db.iom_sides())["JBOD01"]
    assert (r["iom1"]["state"], r["iom2"]["state"]) == ("ok", "ok")


# ---------------------------------------------------------------- leases
def test_bmc_on_a_pool_lease_fails_bmc_leased(tmp_path):
    ctx = make(tmp_path)
    ctx.kea.leases_of["02fa25010012"] = {"ip": "172.32.200.5", "expire": None}
    run_discover(ctx)
    d = det(ctx, "L37", "bmc-leased")
    assert d["error"] == "leased 172.32.200.5, reserved 172.32.37.11"
    assert any("next DHCP renewal" in n for n in d["notes"])
    assert not calls(ctx, "power_action", "L37")


def test_expired_lease_does_not_count(tmp_path):
    ctx = make(tmp_path)
    ctx.kea.leases_of["02fa25010012"] = {"ip": "172.32.37.11", "expire": "2020-01-01T00:00:00+00:00"}
    run_discover(ctx)
    assert det(ctx, "L37", "bmc-leased")["error"] == "lease 172.32.37.11 expired"


# ---------------------------------------------------------------- Qualify's boot phase
def test_qualify_skips_reboot_when_boot_id_unchanged(tmp_path, profiles):
    ctx = make_ctx(tmp_path, keep_boots=True)
    run_qualify(ctx, positions=["L37"])
    assert not calls(ctx, "boot_live")
    d = det(ctx, "L37", "pxe-live")
    assert v(ctx, "L37", "pxe-live") == "pass" and d["reboot"] == "skipped"
    assert d["boot_id"] == det(ctx, "L37", "host-ssh")["boot_id"]
    assert v(ctx, "L37", "agent-reachable") == "pass" and v(ctx, "L37", "console") == "pass"


def test_qualify_reboots_when_boot_id_changed(tmp_path, profiles):
    ctx = make_ctx(tmp_path, keep_boots=True)
    ctx.rungs.forget_boots()
    run_qualify(ctx, positions=["L37"])
    assert [c[1] for c in calls(ctx, "boot_live")] == ["L37"]
    assert det(ctx, "L37", "pxe-live")["rebooted_because"].startswith("boot_id changed since Discover")


def test_qualify_reboots_when_discover_did_not_prove_a_boot(tmp_path, profiles):
    ctx = make_ctx(tmp_path, keep_boots=True)
    from flax_rungs.result import RungResult
    ctx.db.finish_run(ctx.db.start_run("L37", "discover", "host-ssh"),
                      RungResult("fail", {"error": "x"}))
    run_qualify(ctx, positions=["L37"])
    assert det(ctx, "L37", "pxe-live")["rebooted_because"] == \
        "Discover's host-ssh did not prove a live boot"


def test_qualify_gate_keeps_identity_check(tmp_path, profiles):
    ctx = make_ctx(tmp_path)
    ctx.db.set_macs("R37", host_mac=None, bmc_mac=None, iom_macs=[])
    run_qualify(ctx, positions=["R37"])
    assert det(ctx, "R37", "pxe-live")["error"] == "discover first: no host MAC matched"
    assert not calls(ctx, "boot_live")


def test_old_rows_do_not_open_the_gate(tmp_path, profiles):
    from flax_rungs.result import RungResult
    ctx = make_ctx(tmp_path, discover=False)
    for r in ("reserve", "bmc-reach"):
        ctx.db.finish_run(ctx.db.start_run("L37", "discover", r), RungResult("pass", {}))
    ctx.db.set_macs("L37", host_mac="02fa25010010", bmc_mac="02fa25010012", iom_macs=[])
    run_qualify(ctx, positions=["L37"])
    assert det(ctx, "L37", "pxe-live")["error"].startswith("discover first: bmc-reserved not run")


# ---------------------------------------------------------------- server / old rows
def test_state_lists_post_steps_and_old_rows_from_history(tmp_path):
    from flax_rack.server import DISCOVER_RUNGS, Rack, rack_state
    from flax_rungs.result import RungResult
    ctx = make(tmp_path)
    ctx.db.finish_run(ctx.db.start_run("R37", "discover", "slot-match"),
                      RungResult("pass", {"status": "found", "note": ""}))
    ctx.db.finish_run(ctx.db.start_run("R37", "discover", "reserve"), RungResult("pass", {}))
    assert DISCOVER_RUNGS == dl.DISCOVER_STEPS
    r = Rack.__new__(Rack)
    r.g, r.db, r.dir = ctx.g, ctx.db, tmp_path
    import threading
    r.lock = type("L", (), {"locked": lambda self: False, "busy": lambda self: False})()
    r.stage = None
    s = rack_state(r)
    r37 = next(d for d in s["devices"] if d["pos"] == "R37")
    assert set(r37["runs"]) == {"slot-match", "reserve"}              # history still shown
    assert "link_seen" not in r37 and r37["live_links"]["mezz0"]["state"] == "unknown"
    js = (dl.__file__.rsplit("/", 1)[0] + "/web/rackpost.js")
    src = open(js).read()
    assert "discover_old_rungs" in src and "d.headline" in src


# ---------------------------------------------------------------- fix wave: positions, cancel
def test_positions_run_only_those_devices(tmp_path):
    ctx = make(tmp_path)
    run_discover(ctx, positions=["L37", "JBOD01.iom2", "_sw-A.bmc"])
    keys = {k for k, _ in ctx.db.latest_runs()}
    assert {"L37", "JBOD01", "JBOD01.iom1", "_sw-A"} <= keys
    assert not keys & {"C37", "R37", "L05", "R05", "_sw-B", "_sw-C"}
    assert {c[1] for c in calls(ctx, "power_action")} <= {"L37"}
    assert any("discover started: L37, JBOD01.iom2, _sw-A.bmc" in e["text"]
               for e in ctx.db.events(30))


def test_positions_must_name_rack_devices():
    g = load_geometry(FIX)
    assert [d.pos for d in dl.wanted(g, ["JBOD01.iom1"])[0]] == ["JBOD01"]
    assert {s.id for s in dl.wanted(g, ["_sw-B.bmc", "C"])[1]} == {"B", "C"}
    for bad in (["NOPE"], ["L37.bmc"], ["_sw-L37"], ["_sw-A.iom1"], ["../x"]):
        with pytest.raises(ValueError, match="not a device or switch"):
            dl.wanted(g, bad)


def test_cancel_stops_the_walk_before_power_on(tmp_path):
    ctx = make(tmp_path)
    real = ctx.rungs.tp_fru

    def fru(*a, **k):                    # Cancel pressed while the serial step reads
        ctx.cancel.set()
        return real(*a, **k)
    ctx.rungs.tp_fru = fru
    run_discover(ctx, positions=["L37"])
    got = dict(runs(ctx, "L37"))
    assert got["serial"] == "pass"
    for s in dl.DISCOVER_STEPS[dl.DISCOVER_STEPS.index("power-on"):]:
        assert got[s] == "skip" and det(ctx, "L37", s)["reason"] == "cancelled", s
    assert not calls(ctx, "power_action") and not calls(ctx, "set_boot_pxe")
    assert any(e["text"].startswith("discover cancelled") for e in ctx.db.events(30))


# ---------------------------------------------------------------- fix wave: switch logins
def _spy(ctx, name):
    seen, real = [], getattr(ctx.rungs, name)

    def f(ip, *a, **k):
        seen.append(ip)
        return real(ip, *a, **k)
    setattr(ctx.rungs, name, f)
    return seen


def test_extra_macs_on_the_turtle_port_fail_and_ssh_nothing(tmp_path):
    ctx = make(tmp_path)
    ctx.rungs.sw_macs["A"]["stray"] = "02aabbccdd01"      # a trunk/uplink port: 3 MACs
    ctx.kea.dyn["02aabbccdd01"] = "172.26.0.99"
    bmc, sonic = _spy(ctx, "wedge_bmc"), _spy(ctx, "read_syseeprom")
    run_discover(ctx, positions=["A"])
    d = det(ctx, "_sw-A", "switchportlink")
    assert d["headline"].startswith("unexpected MACs on Gi1/0/18: ") and "02:aa:bb:cc:dd:01" in d["headline"]
    assert bmc == [] and sonic == []
    assert [v for r, v in runs(ctx, "_sw-A")][1:] == ["skip"] * (len(dl.DISCOVER_STEPS) - 1)


def test_switch_logins_go_only_to_active_leases(tmp_path):
    ctx = make(tmp_path)
    m = ctx.rungs.sw_macs["A"]
    stale_bmc, stale_sonic = ctx.kea.dyn[m["bmc"]], ctx.kea.dyn[m["sonic"]]
    real = ctx.kea.leases

    def leases(macs):
        out = real(macs)
        if m["bmc"] in out:              # expired: Kea may have handed its IP on
            out[m["bmc"]] = {**out[m["bmc"]], "expire": "2020-01-01T00:00:00+00:00"}
        if m["sonic"] in out:            # declined (state 1), expire in the future
            out[m["sonic"]] = {**out[m["sonic"]], "state": 1, "expire": "2999-01-01T00:00:00+00:00"}
        return out
    ctx.kea.leases = leases
    bmc, sonic = _spy(ctx, "wedge_bmc"), _spy(ctx, "read_syseeprom")
    run_discover(ctx, positions=["A"])
    assert stale_bmc not in bmc and stale_sonic not in sonic
    assert bmc == [] and sonic == []
    assert dict(runs(ctx, "_sw-A"))["bmc-mac-seen"] == "fail"
    assert dict(runs(ctx, "_sw-A"))["serial"] == "skip"


def test_lease_active_needs_state_0_and_a_future_expire():
    fut, past = "2999-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"
    now = time.time()
    assert dl.lease_active({"ip": "x", "state": 0, "expire": fut}, now)
    assert not dl.lease_active({"ip": "x", "state": 1, "expire": fut}, now)
    assert not dl.lease_active({"ip": "x", "state": 0, "expire": past}, now)
    assert not dl.lease_active({"ip": "x", "state": 0, "expire": "garbage"}, now)
    assert not dl.lease_active(None, now)


def test_installed_os_node_counts_discovered_without_power_or_pxe(tmp_path):
    from flax_rack.server import _skip_reason
    ctx = make(tmp_path, live={"L37"})
    ctx.rungs.installed.add("L37")          # a deployed node: up on Ubuntu 24.04, no BOOTIF
    run_discover(ctx)
    assert not calls(ctx, "power_action", "L37") and not calls(ctx, "set_boot_pxe", "L37")
    po = det(ctx, "L37", "power-on")
    assert v(ctx, "L37", "power-on") == "pass" and po["installed_os"] is True
    assert po["headline"] == "up on its installed OS: no power action"
    lr = ctx.db.latest_runs()
    for s in ("tftp-seen", "ipxe-seen", "live-iso-seen"):
        assert v(ctx, "L37", s) == "skip" and _skip_reason(lr[("L37", s)]) == "n/a", s
    hs = det(ctx, "L37", "host-ssh")
    assert v(ctx, "L37", "host-ssh") == "pass" and hs["headline"].startswith("installed OS (Ubuntu 24.04)")
    assert v(ctx, "L37", "bmc-ready") == "pass"
    assert ("power_action", "C37", "on") in ctx.rungs.calls        # the others still boot


def test_installed_os_probe_needs_no_sudo(tmp_path):
    """A deployed node whose flax user has no NOPASSWD yet is still seen as installed:
    the probes read /proc and /etc only, never through sudo (no cycle of a fresh node)."""
    ctx = make(tmp_path, live={"L37"})
    ctx.rungs.installed.add("L37")
    ctx.rungs.sudo_prompt.add("L37")
    run_discover(ctx)
    assert not calls(ctx, "power_action", "L37")
    assert det(ctx, "L37", "power-on")["installed_os"] is True
    assert det(ctx, "L37", "tftp-seen")["na"] is True


def test_ladder_rungs_record_seen_at_epochs(tmp_path):
    ctx = make(tmp_path)
    run_discover(ctx)
    for s in ("host-pinged", "host-ssh", "bmc-ready"):
        d = det(ctx, "C37", s)
        assert v(ctx, "C37", s) == "pass", s
        assert isinstance(d["seen_at"], float) and d["seen_at"] > 1e9, s
    assert det(ctx, "C37", "power-on")["power_on_at"] > 1e9


def test_already_installed_records_kernel_boot(tmp_path):
    ctx = make(tmp_path, live={"L37"})
    ctx.rungs.installed.add("L37")
    run_discover(ctx)
    po = det(ctx, "L37", "power-on")
    assert po["installed_os"] is True
    assert isinstance(po["kernel_boot"], float) and po["kernel_boot"] > 1e9
