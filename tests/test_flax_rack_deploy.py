import json, threading
from pathlib import Path
import pytest
pytest.importorskip("flax_rack.deploy")
from flax_rack import deploy, qualify
from flax_rack.deploy import (GateError, deploy_passed, deploy_rungs, deploy_why, gate_why,
                              run_deploy)
from flax_rack.fakes import _IFN
from flax_rack.geometry import load_geometry
from flax_rack.qualify import run_qualify
from flax_rungs.bond import HA_MODE
from flax_rungs.result import RungResult
from tests.test_flax_rack_geometry import FIX
from tests.test_flax_rack_qualify import make_ctx, profiles  # noqa: F401 -- fixture

HOSTS = ("L37", "C37", "R37", "L05", "R05")
POP_WAIVE = {"R05": {"rungs": ["population-check"],
                     "reason": "extra SATA SSD missing (BOM 801-0025); operator approved"}}


def v(ctx, pos, rung):
    return (ctx.db.latest_runs().get((pos, rung)) or {}).get("verdict")


def det(ctx, pos, rung):
    return ctx.db.latest_runs()[(pos, rung)]["detail"]


def calls(ctx, name):
    return [c for c in ctx.rungs.calls if c[0] == name]


def qualified(tmp_path, g=None, **rk):
    """A rack that went through Discover + Qualify on FakeRungs (rk: FakeRungs knobs)."""
    c = make_ctx(tmp_path, g, **rk)
    run_qualify(c)
    c.rungs.calls.clear()
    return c


@pytest.fixture
def ctx(tmp_path, profiles):
    return qualified(tmp_path)


def events(ctx):
    return [e["text"] for e in ctx.db.events(60)]


# ---------------------------------------------------------------- happy path
def test_happy_path_walks_every_phase_in_lockstep(ctx):
    run_deploy(ctx, who="op")
    assert deploy_passed(ctx.db, ctx.g) == (True, [])
    for pos in HOSTS:
        assert deploy_rungs(ctx.g.device(pos)) == ["install", "firstboot", "install-console",
                                                   "flax-user", "ha-config", "ha-test",
                                                   "deploy-iperf", "deploy-stress"]
        for rung in deploy_rungs(ctx.g.device(pos)):
            assert v(ctx, pos, rung) == "pass", (pos, rung, det(ctx, pos, rung))
    assert deploy_rungs(ctx.g.device("JBOD01")) == []
    names = [c[0] for c in ctx.rungs.calls]
    # barriers: every install before any first boot, flax_user once, then bonds, one ha-test
    last_install = max(i for i, n in enumerate(names) if n == "install")
    assert last_install < names.index("firstboot")
    assert max(i for i, n in enumerate(names) if n == "firstboot") < names.index("flax_user")
    assert names.index("flax_user") < names.index("bond_config")
    assert max(i for i, n in enumerate(names) if n == "bond_config") < names.index("ha_test")
    assert names.count("flax_user") == 1 and names.count("ha_test") == 1
    # then one iperf mesh (after ha-test) and one stress wave for the whole rack
    assert names.index("ha_test") < names.index("mesh") < names.index("stress")
    assert names.count("mesh") == 1 and names.count("stress") == 1
    # SOL: started before the install, stopped after the first boot
    for pos in HOSTS:
        i_start = ctx.rungs.calls.index(("sol_start", pos))
        i_inst = next(i for i, c in enumerate(ctx.rungs.calls) if c[:2] == ("install", pos))
        i_fb = next(i for i, c in enumerate(ctx.rungs.calls) if c[:2] == ("firstboot", pos))
        i_stop = ctx.rungs.calls.index(("sol_stop", pos))
        assert i_start < i_inst < i_fb < i_stop
    # pxeboot hostname = the Kea reservation's name; firstboot expects the discovered MAC
    assert {c[1]: c[2] for c in calls(ctx, "install")}["C37"] == "rr101-c37"
    assert all(c[2] for c in calls(ctx, "firstboot"))
    assert calls(ctx, "flax_user")[0][1] == sorted(HOSTS)
    (ht,) = calls(ctx, "ha_test")
    assert ht[1] == "L05" and ht[2] == sorted(HOSTS)
    assert ctx.db.deploy_progress() is None
    ha = ctx.db.ha_mode()
    assert ha["mode"] == HA_MODE == "active-backup"
    assert sorted(ha["devices"]) == sorted(HOSTS) and ha["missing"] == []
    assert ha["notes"] and not any("MLAG" in n for n in ha["notes"])
    assert events(ctx)[0].startswith("deploy done; deployed: ")


def test_artifacts_saved_under_the_deploy_run(ctx):
    run_deploy(ctx)
    lr = ctx.db.latest_runs()
    con = lr[("L37", "install-console")]
    rel = con["artifacts"]["sol"]
    assert rel.startswith("artifacts/L37/d") and rel.endswith("/install-console/sol.txt")
    text = (ctx.dir / rel).read_text()
    assert "Install Ubuntu 24.04" in text and "rr101-l37 login" in text   # install + first boot
    assert text.startswith("=== flax-post mark rackpost-deploy ===")   # the relay's file, marked
    assert con["detail"]["findings"] == 0 and "flax-post-sol" in con["detail"]["capture"]["source"]
    ins = lr[("L37", "install")]["artifacts"]["hostname-rr101-l37.ipxe"]
    assert (ctx.dir / ins).read_text().startswith("#!ipxe")
    assert "ansible-playbook" in lr[("L37", "flax-user")]["artifacts"]


def test_ha_config_inputs_from_qualify_evidence(ctx):
    run_deploy(ctx)
    b = {c[1]: c[2:] for c in calls(ctx, "bond_config")}
    macs = ctx.rungs._macs("C37")
    # A-side fabric formula with the address plan's host octet; ports by the live MACs
    assert b["C37"] == ("10.201.37.51/16", macs[_IFN["pcie0"]], macs[_IFN["pcie1"]])
    assert b["R05"][0] == "10.201.5.101/16"


def test_cross_cabled_primary_is_the_port_lldp_saw_on_wedge_a(tmp_path, profiles):
    raw = json.loads(Path(FIX).read_text())
    r05 = next(d for d in raw["devices"] if d["pos"] == "R05")
    r05["links"]["pcie0"], r05["links"]["pcie1"] = r05["links"]["pcie1"], r05["links"]["pcie0"]
    p = tmp_path / "g.json"; p.write_text(json.dumps(raw))
    c = qualified(tmp_path, load_geometry(p))
    assert gate_why(c.db.latest_runs(), c.g.device("R05")) is None
    run_deploy(c)
    b = {x[1]: x[2:] for x in calls(c, "bond_config")}
    macs = c.rungs._macs("R05")
    # P1 is on wedge A here: it is the primary, not the P0 label
    assert b["R05"][1] == macs[_IFN["pcie1"]] and b["R05"][2] == macs[_IFN["pcie0"]]


# ---------------------------------------------------------------- gate + waivers
def test_rack_wide_gate_needs_every_host_passed(tmp_path, profiles):
    c = qualified(tmp_path, fail_pxe={"R37"})
    with pytest.raises(GateError, match=r"rack-wide.*R37 qualify not passed: pxe-live fail"):
        run_deploy(c)
    assert not calls(c, "install") and not any(r == "install" for (_p, r) in c.db.latest_runs())


def test_positions_run_records_rack_incomplete(tmp_path, profiles):
    c = qualified(tmp_path, fail_pxe={"R37"})
    with pytest.raises(GateError, match="R37"):
        run_deploy(c, positions=["R37", "L37"])
    run_deploy(c, positions=["L37", "L05"])
    assert v(c, "L37", "ha-test") == "pass" and v(c, "R37", "install") is None
    assert any(t.startswith("rack incomplete: R37 (qualify not passed: pxe-live fail") and
               "L37" not in t for t in events(c))
    assert sorted(x[1] for x in calls(c, "install")) == ["L05", "L37"]


def test_waived_rung_passes_the_gate_and_is_recorded(tmp_path, profiles):
    c = qualified(tmp_path, pop_fail={"R05"})
    assert v(c, "R05", "population-check") == "fail"
    with pytest.raises(GateError, match="R05 qualify not passed: population-check fail"):
        run_deploy(c, positions=["R05", "L05"])
    run_deploy(c, positions=["R05", "L05"], waive=POP_WAIVE, who="dbahi")
    assert v(c, "R05", "ha-test") == "pass" and deploy_why(c.db.latest_runs(), c.g).get("R05") is None
    (w,) = c.db.waivers(history=True)
    assert w["pos"] == "R05" and w["rungs"] == ["population-check"] and w["who"] == "dbahi" \
        and w["stage"] == "deploy" and w["reason"].startswith("extra SATA SSD") and w["at"]
    assert c.db.waivers()["R05"] == w
    assert any(t.startswith("gate waiver R05: population-check fail -- extra SATA") and
               t.endswith("(by dbahi)") for t in events(c))


def test_unwaived_failing_rung_still_blocks(tmp_path, profiles):
    c = qualified(tmp_path, pop_fail={"R05"}, iperf_fail={"R05": "A"})
    assert v(c, "R05", "iperf-mesh") == "fail"
    with pytest.raises(GateError, match="R05 qualify not passed: iperf-mesh fail") as e:
        run_deploy(c, positions=["R05"], waive=POP_WAIVE)
    assert "population-check" not in str(e.value)       # that one is waived
    assert c.db.waivers() == {}                          # a refused run records nothing
    assert not calls(c, "install")


def test_unmeasured_rung_blocks_unless_named(tmp_path, profiles):
    c = qualified(tmp_path)
    c.db.finish_run(c.db.start_run("R37", "qualify", "iperf-mesh"),
                    RungResult("unmeasured", {"reason": "server not ready"}))
    with pytest.raises(GateError, match="R37 qualify not passed: not measured: iperf-mesh"):
        run_deploy(c, positions=["R37"])
    run_deploy(c, positions=["R37"], waive={"R37": {"rungs": ["iperf-mesh"], "reason": "x"}})
    assert v(c, "R37", "flax-user") == "pass"


@pytest.mark.parametrize("waive,msg", [
    ({"L37": {"rungs": ["population-check"], "reason": "x"}}, "not a device of this Deploy"),
    ({"R05": {"rungs": ["nope"], "reason": "x"}}, "not a Qualify rung of R05: nope"),
    ({"R05": {"rungs": ["population-check"]}}, "a reason is required"),
    ({"R05": {"rungs": ["population-check"], "reason": "  "}}, "a reason is required"),
    ({"R05": {"rungs": [], "reason": "x"}}, "non-empty list"),
    ({"R05": ["population-check"]}, "want"),
    (["R05"], "waive must be"),
])
def test_bad_waivers_refused(ctx, waive, msg):
    with pytest.raises(GateError, match=msg):
        run_deploy(ctx, positions=["R05"], waive=waive)
    assert ctx.db.waivers() == {} and not calls(ctx, "install")


def test_rack_wide_deploy_carries_waivers(tmp_path, profiles):
    c = qualified(tmp_path, pop_fail={"R05"})
    with pytest.raises(GateError):
        run_deploy(c)
    run_deploy(c, waive=POP_WAIVE)
    assert deploy_passed(c.db, c.g) == (True, [])


# ---------------------------------------------------------------- reinstall / --from
def test_reinstall_needs_the_order_id_else_from_firstboot(ctx):
    run_deploy(ctx)
    ctx.rungs.calls.clear()
    with pytest.raises(GateError, match=r"already installed: .*L37.*reinstall RRORD-101.*--from firstboot"):
        run_deploy(ctx, positions=["L37"])
    with pytest.raises(GateError, match="already installed"):
        run_deploy(ctx, positions=["L37"], reinstall="rrord-101")
    assert not calls(ctx, "install")
    run_deploy(ctx, positions=["L37"], reinstall="RRORD-101")
    assert [c[1] for c in calls(ctx, "install")] == ["L37"]
    ctx.rungs.calls.clear()
    run_deploy(ctx, positions=["L37"], from_phase="firstboot")
    assert not calls(ctx, "install")
    # the capture spans the first boot only, and is still scanned
    assert ctx.rungs.calls.index(("sol_start", "L37")) < \
        next(i for i, c in enumerate(ctx.rungs.calls) if c[0] == "firstboot")
    assert v(ctx, "L37", "install-console") == "pass" and v(ctx, "L37", "ha-test") == "pass"
    with pytest.raises(GateError, match="from must be one of"):
        run_deploy(ctx, positions=["L37"], from_phase="ha-test")


def test_from_firstboot_needs_a_passed_install(ctx):
    with pytest.raises(GateError, match=r"no passed install recorded for L37; deploy them from install"):
        run_deploy(ctx, positions=["L37"], from_phase="firstboot")
    assert not ctx.rungs.calls


def test_restart_mid_install_removes_the_hostname_file(ctx, tmp_path):
    """A daemon restart kills an install with no finally: the next daemon's Rack removes
    the hostname file so a power-on cannot reinstall."""
    from flax_rack.server import Rack
    from flax_rungs import pxeboot
    pxe = tmp_path / "pxe"
    pxe.mkdir()
    ctx.db.start_run("L37", "deploy", "install")            # left running by the dead daemon
    (pxe / "hostname-rr101-l37.ipxe").write_text("#!ipxe\n")
    (pxe / "hostname-rr101-c37.ipxe").write_text("#!ipxe\n")   # no interrupted install: kept
    Rack(tmp_path, ctx.g, pxe_dir=str(pxe))
    assert ctx.db.latest_runs()[("L37", "install")]["status"] == "interrupted"
    assert not pxeboot.exists("rr101-l37", pxe_dir=str(pxe))
    assert pxeboot.exists("rr101-c37", pxe_dir=str(pxe))
    assert any("L37: install interrupted by a restart; hostname-rr101-l37.ipxe removed" in t
               for t in events(ctx))


# ---------------------------------------------------------------- failures
def test_install_failure_skips_downstream_rack_continues(ctx):
    ctx.rungs.install_fail = {"R37"}
    run_deploy(ctx)
    assert v(ctx, "R37", "install") == "fail"
    assert "never powered off" in det(ctx, "R37", "install")["error"]
    assert det(ctx, "R37", "install")["sol_tail"]
    for rung in ("firstboot", "flax-user", "ha-config", "ha-test"):
        assert v(ctx, "R37", rung) == "skip" and det(ctx, "R37", rung)["reason"] == \
            "failed in phase install", rung
    # its SOL is stopped at the install barrier and scanned (the evidence of the hang)
    assert v(ctx, "R37", "install-console") == "pass"
    assert ctx.rungs.calls.index(("sol_stop", "R37")) < \
        next(i for i, c in enumerate(ctx.rungs.calls) if c[0] == "firstboot")
    assert ("firstboot", "R37") not in [c[:2] for c in ctx.rungs.calls]
    assert "R37" not in calls(ctx, "flax_user")[0][1]
    assert v(ctx, "L37", "ha-test") == "pass"
    assert deploy_passed(ctx.db, ctx.g) == (False, ["R37"])


def test_console_findings_fail_install_console_but_never_gate(ctx):
    ctx.rungs.sol_findings = {"C37"}
    run_deploy(ctx)
    d = det(ctx, "C37", "install-console")
    assert v(ctx, "C37", "install-console") == "fail" and d["findings"] == 1
    assert d["counts"] == {"I/O error": 1} and any("Buffer I/O error" in r[1] for r in d["rows"])
    assert v(ctx, "C37", "ha-test") == "pass"            # the later phases ran
    assert deploy_why(ctx.db.latest_runs(), ctx.g)["C37"] == "install-console fail"


def test_firstboot_refused_names_the_user(ctx):
    ctx.rungs.firstboot_fail = {"L37"}
    run_deploy(ctx)
    assert v(ctx, "L37", "firstboot") == "fail"
    assert "ssh as flax@172.32.37.1 refused" in det(ctx, "L37", "firstboot")["error"]
    assert v(ctx, "L37", "flax-user") == "skip"


def test_ha_config_without_qualify_links_fails(ctx):
    lr = ctx.db.latest_runs()
    ha = lr[("R37", "ha-path")]
    ctx.db.finish_run(ctx.db.start_run("R37", "qualify", "ha-path"),
                      RungResult("pass", {**ha["detail"], "links": []}))
    run_deploy(ctx)
    assert v(ctx, "R37", "ha-config") == "fail"
    assert "no port seen on wedge A" in det(ctx, "R37", "ha-config")["error"]
    assert v(ctx, "R37", "ha-test") == "skip" and v(ctx, "L37", "ha-test") == "pass"


def test_server_failed_in_this_run_fails_every_client(ctx):
    ctx.rungs.bond_fail = {"L05"}
    run_deploy(ctx)
    assert not calls(ctx, "ha_test")
    for pos in ("L37", "C37", "R37", "R05"):
        assert v(ctx, pos, "ha-test") == "fail"
        assert det(ctx, pos, "ha-test")["error"] == "server L05 not deployed: failed in phase ha-config"
    assert v(ctx, "L05", "ha-test") == "skip"
    assert ctx.db.ha_mode() is None


def test_server_outside_positions_joins_when_its_ha_config_passed(ctx):
    run_deploy(ctx)
    ctx.rungs.calls.clear()
    run_deploy(ctx, positions=["R37"], from_phase="firstboot")
    (ht,) = calls(ctx, "ha_test")
    assert ht[1] == "L05" and ht[2] == ["L05", "R37"]
    assert not [c for c in calls(ctx, "bond_config") if c[1] == "L05"]   # not reconfigured


def test_server_outside_positions_not_deployed_fails_clients(ctx):
    run_deploy(ctx, positions=["R37"])
    assert not calls(ctx, "ha_test")
    assert det(ctx, "R37", "ha-test")["error"] == "server L05 not deployed: its ha-config has not passed"
    assert v(ctx, "L05", "ha-test") is None


def test_ha_mode_lists_the_missing(ctx):
    ctx.rungs.hatest_fail = {"C37"}
    run_deploy(ctx)
    ha = ctx.db.ha_mode()
    assert ha["mode"] == HA_MODE and ha["missing"] == ["C37"] and "C37" not in ha["devices"]


def test_cancel_during_install_stops_at_the_barrier(ctx, monkeypatch):
    real = ctx.rungs.install

    def inst(*a, **k):
        ctx.cancel.set()                     # pressed while the first install runs
        return real(*a, **k)
    ctx.rungs.install = inst
    monkeypatch.setattr(deploy, "MAX_PARALLEL", 1)
    run_deploy(ctx)
    (first,) = [c[1] for c in calls(ctx, "install")]
    assert v(ctx, first, "install") == "pass"
    for pos in HOSTS:
        if pos != first:
            assert det(ctx, pos, "install")["reason"] == "cancelled"
            assert v(ctx, pos, "install-console") == "skip"
        for rung in ("firstboot", "flax-user", "ha-config", "ha-test"):
            assert v(ctx, pos, rung) == "skip" and det(ctx, pos, rung)["reason"] == "cancelled"
    # the installed one's console is still closed out
    assert v(ctx, first, "install-console") == "pass"
    assert not calls(ctx, "firstboot") and events(ctx)[0].startswith("deploy cancelled")


def test_progress_mid_run(ctx):
    seen = []
    real = ctx.rungs.flax_user

    def fu(*a, **k):
        seen.append(ctx.db.deploy_progress())
        return real(*a, **k)
    ctx.rungs.flax_user = fu
    run_deploy(ctx)
    assert seen[0]["phase"] == "user" and seen[0]["n"] == 3 and seen[0]["of"] == 7
    assert ctx.db.deploy_progress() is None


def test_closed_order_refused(ctx):
    ctx.db.set_closed()
    with pytest.raises(RuntimeError):
        run_deploy(ctx)


# ---------------------------------------------------------------- CLI
def _cli_dir(tmp_path, monkeypatch, **rk):
    from flax_rack import server
    d = tmp_path / "o"; d.mkdir()
    (d / "geometry.json").write_text(Path(FIX).read_text())
    holder = {}

    def factory(rd, g, db):
        if "c" not in holder:
            holder["c"] = make_ctx(rd, **rk)
            run_qualify(holder["c"])
        return holder["c"]
    monkeypatch.setattr(server, "default_context_factory", factory)
    monkeypatch.chdir(d)
    return holder


def test_deploy_cli_waive(tmp_path, profiles, monkeypatch, capsys):
    from flax_rack import __main__ as cli
    h = _cli_dir(tmp_path, monkeypatch, pop_fail={"R05"})
    with pytest.raises(SystemExit, match="--confirm <order id>"):
        cli.main(["deploy"])
    with pytest.raises(SystemExit, match="needs --reason"):
        cli.main(["deploy", "--pos", "R05", "--waive", "R05:population-check"])
    with pytest.raises(SystemExit, match="want POS:RUNG"):
        cli.main(["deploy", "--pos", "R05", "--waive", "R05", "--reason", "x"])
    with pytest.raises(SystemExit, match="population-check fail"):
        cli.main(["deploy", "--pos", "R05", "L05"])
    rc = cli.main(["deploy", "--pos", "R05", "L05", "--waive", "R05:population-check",
                   "--reason", "SSD missing; approved", "--by", "dbahi"])
    out = capsys.readouterr().out
    assert rc == 0 and "R05      ha-test          pass" in out and "deploy: deployed" in out
    assert "HA mode delivered: active-backup on " in out
    assert h["c"].db.waivers()["R05"]["who"] == "dbahi"
    with pytest.raises(SystemExit, match="already installed"):
        cli.main(["deploy", "--pos", "L05"])
    assert cli.main(["deploy", "--pos", "L05", "--from", "firstboot"]) == 0
    assert cli.main(["deploy", "--pos", "L05", "--reinstall", "RRORD-101"]) == 0


# ---------------------------------------------------------------- server
from tests.test_flax_rack_qualify import QFactory   # noqa: E402
from tests.test_flax_rack_server import RACK, _order_dir, _start, _wait_idle, get_json, post  # noqa: E402


class DFactory(QFactory):
    """One FakeRungs per rack (Discover's boots carry into Qualify and Deploy)."""
    def __init__(self, **rk):
        super().__init__()
        self.rk, self.rungs = rk, None

    def __call__(self, rack_dir, g, db):
        c = super().__call__(rack_dir, g, db)
        if self.rungs is None:
            from flax_rack.fakes import FakeRungs
            from tests.test_flax_rack_stages import dell_fdb
            self.rungs = FakeRungs(c.g, fdb_runner=dell_fdb, iperf_fail={}, **self.rk)
        c.rungs = self.rungs
        return c


@pytest.fixture
def dsrv(tmp_path, profiles):
    _order_dir(tmp_path)
    f = DFactory(pop_fail={"R05"})
    s = _start(tmp_path, f)
    s.factory = f
    assert post(s, "/rr101/api/stage/discover", RACK).status == 202
    _wait_idle(s)
    assert post(s, "/rr101/api/stage/qualify", {"confirm": "RRORD-101"}).status == 202
    _wait_idle(s)
    yield s
    s.shutdown(); s.server_close()


def test_deploy_route_confirm_gate_waiver_state(dsrv):
    u = "/rr101/api/stage/deploy"
    for body in ({}, {"confirm": "rrord-101"}, {"confirm": True}):
        r = post(dsrv, u, body)
        assert r.status == 400 and b"order id" in r.body, body
    for body in ({"positions": ["R05"]}, {"positions": ["R05"], "confirm": "RRORD-101"}):
        r = post(dsrv, u, body)
        assert r.status == 400 and b"confirm" in r.body, body
    assert post(dsrv, u, {"positions": ["NOPE"], "confirm": True}).status == 400
    assert post(dsrv, u, {"positions": ["L05"], "confirm": True, "from": "user"}).status == 400
    r = post(dsrv, u, {"confirm": "RRORD-101"})          # rack-wide: R05 POP red
    assert r.status == 409 and b"R05 qualify not passed: population-check fail" in r.body
    s = get_json(dsrv, "/rr101/api/state")
    r05 = next(d for d in s["devices"] if d["pos"] == "R05")
    assert r05["deploy_gate"].startswith("qualify not passed") and \
        r05["gate_blockers"] == ["population-check"] and r05["waiver"] is None
    assert s["deploy_phases"] == [p for p in deploy.PHASES if p != "kind-check"] and s["deploy_from"] == ["install", "firstboot", "iperf", "stress"]
    assert s["deploy"]["progress"] is None and s["deploy"]["ha_mode"] is None
    r = post(dsrv, u, {"positions": ["R05", "L05"], "confirm": True,
                       "waive": {"R05": {"rungs": ["population-check"]}}})
    assert r.status == 409 and b"reason is required" in r.body
    r = post(dsrv, u, {"positions": ["R05", "L05"], "confirm": True, "waive": POP_WAIVE})
    assert r.status == 202, r.body
    assert json.loads(r.body) == {"started": "deploy", "positions": ["L05", "R05"],
                                  "from": "install", "reinstall": False, "waived": ["R05"]}
    _wait_idle(dsrv)
    s = get_json(dsrv, "/rr101/api/state")
    r05 = next(d for d in s["devices"] if d["pos"] == "R05")
    assert r05["deploy_rungs"][0] == "install" and r05["runs"]["ha-test"]["verdict"] == "pass"
    w = r05["waiver"]
    assert w["rungs"] == ["population-check"] and w["active"] is True and \
        w["reason"].startswith("extra SATA SSD") and w["who"].startswith("ui ") and w["at"]
    assert s["deploy"]["waivers"][0]["pos"] == "R05"
    assert s["deploy"]["ha_mode"]["mode"] == HA_MODE
    assert "R05" not in s["deploy"]["why"] and "L37" in s["deploy"]["not_passed"]
    assert r05["runs"]["install-console"]["artifacts"]["sol"].endswith("/install-console/sol.txt")
    # an installed node: reinstall needs the order id
    r = post(dsrv, u, {"positions": ["L05"], "confirm": True})
    assert r.status == 409 and b"already installed: L05" in r.body
    assert post(dsrv, u, {"positions": ["L05"], "confirm": True, "reinstall": "x"}).status == 400
    r = post(dsrv, u, {"positions": ["L05"], "confirm": True, "reinstall": "RRORD-101",
                       "by": "dbahi"})
    assert r.status == 202 and json.loads(r.body)["reinstall"] is True
    _wait_idle(dsrv)
    r = post(dsrv, u, {"positions": ["L05"], "confirm": True, "from": "firstboot"})
    assert r.status == 202
    _wait_idle(dsrv)


# ---------------------------------------------------------------- live HA sides
def test_ha_config_sides_from_live_lldp_else_qualify(ctx):
    run_deploy(ctx)
    for pos in ("L37", "L05", "R05"):       # the wedges' LLDP names them with their MACs
        assert det(ctx, pos, "ha-config")["sides_from"] == "live LLDP", pos
    # the fixture repeats L37's lanes for C37/R37: live LLDP shows nothing for them, the
    # ports are set up and re-read, then Qualify's ha-path decides
    for pos in ("C37", "R37"):
        d = det(ctx, pos, "ha-config")
        assert v(ctx, pos, "ha-config") == "pass" and d["sides_from"] == "Qualify ha-path", pos
        assert [c for c in calls(ctx, "fabric_up") if c[1] == pos]
    assert not [c for c in calls(ctx, "fabric_up") if c[1] in ("L37", "L05", "R05")]


def test_admin_down_ports_are_raised_then_seen_live(ctx):
    ctx.rungs.fabric_down = {"L05"}         # bond never configured: ports admin-down
    run_deploy(ctx)
    macs = ctx.rungs._macs("L05")
    (up,) = [c for c in calls(ctx, "fabric_up") if c[1] == "L05"]
    assert up[2] == sorted([macs[_IFN["pcie0"]], macs[_IFN["pcie1"]]])
    d = det(ctx, "L05", "ha-config")
    assert v(ctx, "L05", "ha-config") == "pass" and d["sides_from"] == "live LLDP"
    assert d["macs"] == {"A": macs[_IFN["pcie0"]], "B": macs[_IFN["pcie1"]]}


def test_live_lldp_one_side_only_fails_no_qualify_fallback(ctx):
    ctx.rungs.pulled = {("B", "Ethernet108")}     # L05's wedge-B lane (28/1) shows nothing
    run_deploy(ctx)
    assert v(ctx, "L05", "ha-config") == "fail"
    assert det(ctx, "L05", "ha-config")["error"].startswith(
        "live LLDP sees only wedge A (Ethernet108 ")


def test_ha_config_names_the_wedge_that_did_not_answer(ctx):
    ctx.rungs.live_down = {"B"}                   # wedge B's ssh read fails
    run_deploy(ctx)
    assert v(ctx, "L05", "ha-config") == "fail"
    assert det(ctx, "L05", "ha-config")["error"].startswith(
        "wedge B did not answer (not read): its side of rr101-l05 is not proven; "
        "live LLDP sees wedge A (Ethernet108 ")


def test_ha_path_waiver_is_harmless_with_live_lldp(ctx):
    lr = ctx.db.latest_runs()
    ctx.db.finish_run(ctx.db.start_run("L05", "qualify", "ha-path"),
                      RungResult("fail", {**lr[("L05", "ha-path")]["detail"], "links": [],
                                          "error": "pcie0: expected A Ethernet108"}))
    run_deploy(ctx, waive={"L05": {"rungs": ["ha-path"], "reason": "cable re-seated after"}})
    assert det(ctx, "L05", "ha-config")["sides_from"] == "live LLDP"
    # ha-test takes the sides from the passed ha-config, not from Qualify
    assert v(ctx, "L05", "ha-test") == "pass" and v(ctx, "C37", "ha-test") == "pass"


def test_live_sides_from_the_poller_snapshot(ctx):
    from types import SimpleNamespace
    from flax_rack.switches import LldpEntry
    snaps = {"A": {"at": 1000.0, "lldp": [LldpEntry("Ethernet95", "rr101-c37", "88:e9:a4:79:f9:a8",
                                                     "enp25s0f0np0"),
                                           LldpEntry("Ethernet99", "rr101-r37", "88:e9:a4:73:a4:90",
                                                     "enp25s0f0np0")]},
             "B": {"at": 1000.0, "lldp": [LldpEntry("Ethernet95", "rr101-c37", "88e9a479f9a9",
                                                     "enp25s0f1np1")]}}
    ctx.links = SimpleNamespace(snapshots=lambda: snaps, clock=lambda: 1010.0, interval_s=30)
    s, now, interval = deploy._snapshots(ctx, ctx.rungs)
    side = deploy.live_sides(ctx.g, s, "C37", now)
    assert side["A"] == {"mac": "88:e9:a4:79:f9:a8", "link": "A Ethernet95",
                         "ifname": "enp25s0f0np0"}
    assert side["B"]["mac"] == "88:e9:a4:79:f9:a9"
    assert deploy.live_sides(ctx.g, s, "C37", 1000.0 + 91) == {}    # stale: nothing live
    assert not calls(ctx, "read_live")                              # the poller's, no ssh


def test_deployed_nodes_stay_put_on_discover(ctx):
    from flax_rack.stages import run_discover
    run_deploy(ctx)
    ctx.rungs.calls.clear()
    run_discover(ctx)
    for pos in HOSTS:
        assert not [c for c in ctx.rungs.calls if c[0] in ("power_action", "set_boot_pxe")
                    and c[1] == pos], pos
        assert det(ctx, pos, "power-on")["headline"] == "up on its installed OS: no power action"
        assert v(ctx, pos, "host-ssh") == "pass"


# ---------------------------------------------------------------- iperf + stress phases
def test_iperf_over_mgmt_and_bond_then_stress_on_every_node(ctx):
    run_deploy(ctx)
    (m,) = calls(ctx, "mesh")
    assert m[1] == "L05" and sorted(m[3]) == sorted(set(HOSTS) - {"L05"})
    d = det(ctx, "L37", "deploy-iperf")
    t = {r[0]: r for r in d["table"]["rows"]}
    assert t["mgmt"][1:2] + t["mgmt"][4:6] == ["eno1", "0.94", "0.94"]
    assert t["bond"][1] == "bond0" and t["bond"][4:6] == ["23.5", "23.5"] and t["bond"][7] == "ok"
    assert d["headline"].startswith("bond 23.5/23.5") and "Gb/s" in d["headline"]
    assert det(ctx, "L05", "deploy-iperf")["served"] == m[3]
    (st,) = calls(ctx, "stress")
    assert st[1] == sorted(HOSTS)
    d = det(ctx, "R05", "deploy-stress")
    assert d["headline"].startswith("stress-ng 600 s rc 0") and d["launch_spread_s"] is not None
    assert any(e.startswith("stress: stress-ng launched on the nodes within") for e in events(ctx))


def test_deploy_iperf_table_bond_lists_both_ports(ctx):
    run_deploy(ctx)
    d = det(ctx, "L37", "deploy-iperf")
    assert d["table"]["cols"][:3] == ["network", "interface", "port"]
    bond = next(r for r in d["table"]["rows"] if r[0] == "bond")
    assert " + " in bond[2] and bond[2].startswith("A ") and " B " in bond[2]
    assert d["table"]["row_cls"] == ["", ""]


def test_deploy_iperf_table_marks_a_below_threshold_row_bad(tmp_path, profiles):
    c = qualified(tmp_path, iperf_fail={"C37": "bond"})
    run_deploy(c)
    d = det(c, "C37", "deploy-iperf")
    rows = d["table"]["rows"]
    i = [r[0] for r in rows].index("bond")
    assert d["table"]["row_cls"][i] == "bad" and rows[i][7] != "ok"


def test_deploy_iperf_table_keeps_the_raw_artifacts():
    arts = {"iperf-bond-tx": "{}", "iperf-bond-rx": "{}"}
    r = RungResult("pass", {"bond": {"tx_bps": 2e10, "rx_bps": 2e10, "speed_bps": 25e9,
                                     "ok": True}}, arts)
    out = deploy._iperf_table(r, {"ifaces": {"bond": ("bond0", "x")}}, {"A": "A Ethernet1"})
    assert out.artifacts == arts and out.detail["table"]["rows"][0][5] == "20.0"


def test_iperf_fail_keeps_the_node_out_of_stress(tmp_path, profiles):
    c = qualified(tmp_path, iperf_fail={"C37": "bond"})
    run_deploy(c)
    assert v(c, "C37", "deploy-iperf") == "fail"
    assert v(c, "C37", "deploy-stress") == "skip"
    assert det(c, "C37", "deploy-stress")["reason"] == "failed in phase iperf"
    assert calls(c, "stress")[0][1] == sorted(set(HOSTS) - {"C37"})
    assert deploy_why(c.db.latest_runs(), c.g)["C37"] == "deploy-iperf fail"


def test_server_out_of_deploy_leaves_iperf_unmeasured(tmp_path, profiles):
    c = qualified(tmp_path)
    c.rungs.hatest_fail = {"L05"}
    run_deploy(c)
    assert not calls(c, "mesh")
    assert v(c, "L37", "deploy-iperf") == "unmeasured"
    assert "server L05 not ready: failed in phase ha-test" in det(c, "L37", "deploy-iperf")["headline"]


def test_new_sel_entry_during_stress_fails_the_device(ctx):
    ctx.rungs.stress_hosts.sel_bad = {"R37"}
    run_deploy(ctx)
    assert v(ctx, "R37", "deploy-stress") == "fail"
    assert "DIMM_B1" in det(ctx, "R37", "deploy-stress")["headline"]
    assert deploy_passed(ctx.db, ctx.g) == (False, ["R37"])


def test_scoped_deploy_server_row_written_once_a_client_measured(ctx):
    run_deploy(ctx)
    before = ctx.db.latest_runs()[("L05", "deploy-iperf")]["id"]
    run_deploy(ctx, positions=["L37"], from_phase="firstboot")
    lr = ctx.db.latest_runs()
    assert lr[("L05", "deploy-iperf")]["id"] > before
    assert lr[("L05", "deploy-iperf")]["detail"]["served"] == ["L37"]
    assert calls(ctx, "stress")[-1][1] == ["L37"]


def test_from_iperf_reruns_just_iperf_and_stress_no_power(tmp_path, profiles):
    c = qualified(tmp_path, iperf_fail={"C37": "bond"})
    c.rungs.firstboot_fail = {"R37"}
    run_deploy(c)
    lr = c.db.latest_runs()
    with pytest.raises(GateError, match=r"deploy from iperf needs .*R37 firstboot fail"):
        run_deploy(c, positions=["R37"], from_phase="iperf")
    c.rungs.calls.clear()
    c.rungs.iperf_fail = {}
    run_deploy(c, positions=["C37"], from_phase="iperf")
    assert v(c, "C37", "deploy-iperf") == "pass" and v(c, "C37", "deploy-stress") == "pass"
    assert {r[0] for r in c.rungs.calls} <= {"mesh", "host_run", "stress"}       # no power, no install
    after = c.db.latest_runs()
    assert after[("C37", "firstboot")]["id"] == lr[("C37", "firstboot")]["id"]
    assert deploy_why(after, c.g).get("C37") is None
    assert not [e for e in events(c) if "skips the Qualify gate" in e]
    rid = c.db.start_run("C37", "qualify", "inventory")       # C37 failed a re-Qualify
    c.db.finish_run(rid, RungResult("fail", {}, {}))
    run_deploy(c, positions=["C37"], from_phase="stress")
    assert len(calls(c, "mesh")) == 1 and len(calls(c, "stress")) == 2
    assert c.db.latest_runs()[("C37", "deploy-iperf")]["id"] == after[("C37", "deploy-iperf")]["id"]
    assert "deploy from stress skips the Qualify gate: deployed rungs re-run on C37 whose " \
        "qualify is inventory fail" in events(c)


def test_deploy_iperf_shows_two_decimals_below_10_gbps():
    from flax_rack.deploy import _iperf_table
    from flax_rungs.result import RungResult
    res = RungResult("fail", {"mgmt": {"tx_bps": 0.95e9, "rx_bps": 0.79e9, "speed_bps": 1e9},
                              "bond": {"tx_bps": 23.46e9, "rx_bps": 23.5e9, "speed_bps": 25e9}})
    t = {r[0]: r for r in _iperf_table(res, None, {}).detail["table"]["rows"]}
    assert t["mgmt"][4:6] == ["0.95", "0.79"] and t["bond"][4:6] == ["23.5", "23.5"]
