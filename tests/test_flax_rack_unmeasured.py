"""rackpost `unmeasured`: a rung whose target did not answer judged nothing -- grey
"not measured", never a pass, never a fault; a real negative result stays `fail`."""
import subprocess
from types import SimpleNamespace

import pytest

pytest.importorskip("flax_rack.qualify")
from flax_rack import firmware as fw
from flax_rack.orderdb import OrderDB
from flax_rack.qualify import device_rungs, qualify_passed, qualify_why, run_qualify
from flax_rack.stages import run_discover
from flax_rack import switches
from flax_rungs import fabric, fwcheck, identity
from flax_switch_sense.driver_sonic import SonicError
from flax_rungs.result import RungResult, not_passed, unmeasured
from tests.test_flax_rack_discover_ladder import make
from tests.test_flax_rack_firmware import Client, _manifest, frows
from tests.test_flax_rack_geometry import FIX
from tests.test_flax_rack_qualify import ctx, det, make_ctx, profiles, v  # noqa: F401


def rc_runner(rc, out="", err="ssh: connect to host x port 22: No route to host"):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)
    return run


# ---------------------------------------------------------------- the verdict
def test_verdict_stored_like_pass_fail_skip(tmp_path):
    from flax_rack.geometry import load_geometry
    db = OrderDB(tmp_path / "order.db"); db.init(load_geometry(FIX))
    rid = db.start_run("L37", "qualify", "pxe-live")
    db.finish_run(rid, unmeasured("no IPMI answer"))
    r = db.latest_runs()[("L37", "pxe-live")]
    assert r["verdict"] == "unmeasured" and r["detail"]["headline"] == "no IPMI answer"
    with pytest.raises(ValueError):
        RungResult("grey", {})
    assert not_passed("pxe-live", "unmeasured") == "not measured: pxe-live"
    assert not_passed("pxe-live", "fail") == "pxe-live fail"
    assert not_passed("pxe-live", None) == "pxe-live not run"


# ---------------------------------------------------------------- rungs
def sonic_rc(rc):
    """A SonicDriver runner whose ssh exits rc (255: never connected)."""
    def run(cmd):
        raise SonicError(f"w: ssh exited {rc}", rc)
    return run


def test_ssh_unreachable_reads_are_unmeasured_real_failures_stay_fail():
    r = switches.read_lldp("w", "sonic", {}, runner=sonic_rc(255))
    assert r.verdict == "unmeasured" and "did not answer ssh" in r.detail["headline"]
    assert switches.read_lldp("w", "sonic", {}, runner=sonic_rc(1)).verdict == "fail"
    assert switches.read_syseeprom("w", "sonic", {}, runner=sonic_rc(255)).verdict == "unmeasured"
    assert switches.read_syseeprom("w", "sonic", {}, runner=sonic_rc(1)).verdict == "fail"
    r = fabric.apply("h", "u", "p", {"ens3f0np0": "10.201.37.1/16"}, runner=rc_runner(255))
    assert r.verdict == "unmeasured" and "did not answer ssh" in r.detail["headline"]
    # the host answered but an address did not stick: a real fail
    r = fabric.apply("h", "u", "p", {"ens3f0np0": "10.201.37.1/16"},
                     runner=rc_runner(0, out="FAIL ens3f0np0\n", err=""))
    assert r.verdict == "fail"


def test_redfish_unreachable_is_unmeasured_off_target_is_fail():
    M = _manifest()
    sl, _ = fwcheck.tp_bmc("ip", [("u", "p")], M.bmc, client_factory=lambda ip, c: Client(None))
    assert sl["phase"] == "unreachable" and "transport error" in sl["fault_reason"]
    a, b = fwcheck.steps("bmc", sl)
    assert (a.verdict, b.verdict) == ("unmeasured", "unmeasured")
    assert (a.detail["state"], b.detail["state"]) == ("cur", "pending")   # Post's own
    sl, _ = fwcheck.tp_bmc("ip", [("u", "p")], M.bmc,
                           client_factory=lambda ip, c: Client("flax-onetree-1.1.3"))
    assert [r.verdict for r in fwcheck.steps("bmc", sl)] == ["pass", "fail"]   # needs_update


def test_report_only_unmeasured_only_when_bmc_did_not_answer():
    sl, _ = fwcheck.report_only(identity._no_answer("IOM x did not answer ssh", "rc=255"))
    assert [r.verdict for r in fwcheck.steps("bmc", sl)] == ["unmeasured", "unmeasured"]
    sl, _ = fwcheck.report_only(RungResult("fail", {"error": "add obmcuser/obmcpass"}))
    assert [r.verdict for r in fwcheck.steps("bmc", sl)] == ["fail", "skip"]


# ---------------------------------------------------------------- Discover
def test_discover_serial_no_ipmi_answer_is_unmeasured_walk_goes_on(tmp_path):
    ctx = make(tmp_path)
    base = ctx.rungs.tp_fru

    def tp(ip, creds, **k):
        if ctx.rungs.ip_role[ip][0] == "R37" and not any(
                c[0] == "power_action" and c[1] == "R37" for c in ctx.rungs.calls):
            return identity._no_answer(f"FRU 0 not read from {ip}: no IPMI answer",
                                       "Unable to establish IPMI v2 session (ciphers 17/3)")
        return base(ip, creds, **k)
    ctx.rungs.tp_fru = tp
    run_discover(ctx)
    assert v(ctx, "R37", "serial") == "unmeasured"
    assert det(ctx, "R37", "serial")["headline"].startswith("FRU 0 not read: no IPMI answer")
    assert v(ctx, "R37", "power-on") == "pass" and v(ctx, "R37", "bmc-ready") == "pass"
    assert "not measured: R37" in ctx.db.events(5)[0]["text"]


def test_discover_bmc_pinged_no_ping_stays_fail(tmp_path):
    ctx = make(tmp_path)
    ctx.rungs.down.add("172.32.37.11")          # L37's BMC: the reachability step judges it
    run_discover(ctx, positions=["L37"])
    assert v(ctx, "L37", "bmc-pinged") == "fail"
    assert det(ctx, "L37", "serial")["reason"] == "bmc-pinged failed"


def test_discover_dell_syseeprom_unreachable(tmp_path):
    ctx = make(tmp_path)
    ctx.rungs.read_syseeprom = lambda ip, nos, cred, runner=None: switches.read_syseeprom(
        ip, nos, cred, runner=sonic_rc(255))
    run_discover(ctx, positions=["_sw-C"])
    assert v(ctx, "_sw-C", "serial") == "unmeasured"
    assert v(ctx, "_sw-C", "host-ssh") == "fail"   # the reachability step itself stays a fail


# ---------------------------------------------------------------- Firmware
def test_firmware_host_reads_unreachable_are_grey_bmc_needs_update_stays(tmp_path):
    ctx = make(tmp_path)
    run_discover(ctx)
    ctx.rungs.fw_host_down = {"R05"}           # the served fake's scenario
    fw.run_firmware(ctx, positions=["R05"])
    got = frows(ctx, "R05")
    assert got[0][:2] == ("bmc-checked", "pass") and got[1][1] == "fail"   # needs_update
    assert [x[1] for x in got[2:]] == ["unmeasured"] * 4
    assert "not measured" not in ctx.db.events(1)[0]["text"]   # needs update wins the line
    assert "needs update: R05" in ctx.db.events(1)[0]["text"]


# ---------------------------------------------------------------- Qualify
def test_pxe_live_no_ipmi_answer_unmeasured_later_phases_say_so(ctx):
    base = ctx.rungs.boot_live
    ctx.rungs.boot_live = lambda bmc_ip, host_ip, creds, **k: (
        unmeasured("power state unknown (no IPMI answer on any credential/cipher); not acting")
        if ctx.rungs.pos_of.get(bmc_ip) == "R37" else base(bmc_ip, host_ip, creds, **k))
    run_qualify(ctx)
    assert v(ctx, "R37", "pxe-live") == "unmeasured"
    assert det(ctx, "R37", "fio")["reason"] == "not measured in phase boot"
    ok, bad = qualify_passed(ctx.db, ctx.g)
    assert not ok and "R37" in bad                    # never a pass
    assert qualify_why(ctx.db.latest_runs(), ctx.g)["R37"] == "not measured: pxe-live"
    assert any(e["text"] == "phase boot: 4 passed, not measured: R37" for e in ctx.db.events(40))


def test_agent_never_answered_is_unmeasured(ctx):
    ctx.rungs.poll = lambda ip, **k: {"agent": RungResult("unmeasured", {
        "error": "timeout", "last": "connection refused",
        "headline": "agent never answered in 1200s: connection refused"})}
    ctx.rungs.stop_agent = lambda *a, **k: RungResult("pass", {})
    run_qualify(ctx, positions=["L37"])
    assert v(ctx, "L37", "agent-stages") == "unmeasured"
    # the watchdog's verdict is carried by the stage the agent was in (no UI row of its
    # own), with the watchdog log as its artifact; the later stages are skipped
    sp = ctx.db.latest_runs()[("L37", "sdr-pre")]
    assert sp["verdict"] == "unmeasured" and "never answered" in sp["detail"]["headline"]
    assert (ctx.dir / sp["artifacts"]["agent-watchdog"]).read_text().startswith("agent-stages unmeasured")
    assert det(ctx, "L37", "inventory")["reason"] == "agent-stages not measured"
    assert det(ctx, "L37", "fio")["reason"] == "not measured in phase inventory"
    assert qualify_why(ctx.db.latest_runs(), ctx.g)["L37"] == "not measured: sdr-pre"


def test_agent_unhealthy_answer_stays_fail(ctx):
    ctx.rungs.poll = lambda ip, **k: {"agent": RungResult("fail", {"error": "agent unhealthy: x"})}
    run_qualify(ctx, positions=["L37"])
    assert v(ctx, "L37", "agent-stages") == "fail"
    assert v(ctx, "L37", "sdr-pre") == "fail" and "unhealthy" in det(ctx, "L37", "sdr-pre")["error"]
    assert det(ctx, "L37", "fio")["reason"] == "failed in phase inventory"


def test_ha_path_unmeasured_only_when_no_wedge_answered(ctx):
    ctx.rungs.read_lldp = lambda ip, nos, cred, runner=None: RungResult(
        "unmeasured", {"error": "rc=255", "headline": f"{ip} did not answer ssh"})
    run_qualify(ctx)
    assert v(ctx, "_rack", "lldp-A") == v(ctx, "_rack", "lldp-B") == "unmeasured"
    assert v(ctx, "L37", "ha-path") == "unmeasured"
    assert det(ctx, "L37", "ha-path")["headline"] in (
        "LLDP not read: A, B did not answer ssh", "LLDP not read: B, A did not answer ssh")


def test_ha_path_one_wedge_answering_still_judges(ctx):
    base = ctx.rungs.read_lldp
    ctx.rungs.read_lldp = lambda ip, nos, cred, runner=None: (
        RungResult("unmeasured", {"error": "rc=255"}) if ip.endswith("B")
        else base(ip, nos, cred, runner=runner))
    run_qualify(ctx)
    assert v(ctx, "L37", "ha-path") == "fail"
    assert det(ctx, "L37", "ha-path")["error"].startswith("LLDP unreadable on B")


def test_unmeasured_never_passes_presence_proved(ctx):
    run_qualify(ctx)
    assert qualify_passed(ctx.db, ctx.g) == (True, [])      # the presence proof
    rid = ctx.db.start_run("L37", "qualify", "iperf-mesh")
    ctx.db.finish_run(rid, unmeasured("server L05 not ready: x"))
    assert qualify_passed(ctx.db, ctx.g) == (False, ["L37"])
    assert qualify_why(ctx.db.latest_runs(), ctx.g) == {"L37": "not measured: iperf-mesh"}


def test_population_check_no_fru0_unmeasured_absent_fru0_fails(ctx):
    from flax_rack import qualify as q
    d = ctx.g.device("L37")
    arts = {"macinv": "2 Processor Version: x\n"}
    r = q.population_rung(ctx, d, arts, (None, "Unable to establish IPMI v2 session"))
    assert r.verdict == "unmeasured" and r.detail["headline"].startswith("FRU 0 not read out of band")
    r = q.population_rung(ctx, d, arts, ("FRU Device Description : Builtin FRU Device (ID 0)\n"
                                         " Device not present\n", None))
    assert r.verdict == "fail"


def test_rack_state_names_why(tmp_path, profiles):
    from flax_rack.server import Rack, rack_state
    c = make_ctx(tmp_path)
    run_qualify(c)
    rid = c.db.start_run("R05", "qualify", "pxe-live")
    c.db.finish_run(rid, unmeasured("no IPMI answer"))
    s = rack_state(SimpleNamespace(g=c.g, db=c.db, dir=c.dir, lock=SimpleNamespace(locked=lambda: False, busy=lambda: False),
                                   stage=None, cancel=SimpleNamespace(is_set=lambda: False)))
    assert s["qualify"]["why"]["R05"] == "not measured: pxe-live"
    assert "R05" in s["qualify"]["not_passed"]
    assert s["firmware_old_rungs"] == []
    assert "fw-versions" not in [r for d in s["devices"] for r in d["qualify_rungs"]]
