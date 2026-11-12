"""rackpost Qualify = loose-node Post's Qualify (plan 2b Task 0b): Post's step names,
Post's judges (flax_post.qualify_rules + shipchecks), and a SOL console capture."""
import json

import pytest

pytest.importorskip("flax_rack.qualify")
from flax_post import blades, qualify_rules, shipchecks
from flax_post.observe import host_qual
from flax_rack import qualify
from flax_rack.qualify import device_rungs, device_verdict, qualify_passed, run_qualify
from flax_rack.server import Rack, rack_state
from flax_rungs.result import RungResult
from tests.test_flax_rack_qualify import ctx, det, make_ctx, profiles, v  # noqa: F401

POST_Q = blades.PHASE_STEPS["Qualify"]


def _inv_patch(ctx, pos, **arts):
    """Replace some inventory artifacts of `pos` in the fake agent's answer."""
    base = ctx.rungs.poll

    def poll(ip, **k):
        out = base(ip, **k)
        if ctx.rungs.pos_of[ip] == pos and "inventory" in out:
            out["inventory"].artifacts.update(arts)
            for k2, val in arts.items():
                if val is None:
                    out["inventory"].artifacts.pop(k2)
        return out
    ctx.rungs.poll = poll


# ---------------------------------------------------------------- names
def test_step_names_are_posts():
    from flax_rack.geometry import load_geometry
    from tests.test_flax_rack_geometry import FIX
    g = load_geometry(FIX)
    for d in (g.device("L37"), g.device("L05")):
        rungs = device_rungs(d)
        # Post's Qualify steps, in Post's order; Post's iperf (to the bang) is the mesh
        assert [r for r in rungs if r in POST_Q] == [s for s in POST_Q if s != "iperf"]
        assert set(rungs) - set(POST_Q) <= {"pxe-live", "agent-stages", "ha-path",
                                            "fabric-addr", "iperf-mesh"}
    assert "pop" not in rungs and "agent-launch" not in rungs


def test_judges_are_posts_own_code():
    # moved, not copied: host_qual's names are the shared module's functions
    assert host_qual.population_check is qualify_rules.population_check
    assert host_qual.qualify_verdict is qualify_rules.qualify_verdict
    assert host_qual.NO_SHIP_RULE is qualify_rules.NO_SHIP_RULE
    assert qualify.ENGINE_CHECKS == ("population-check", *shipchecks.SHIP_CHECKS)


# ---------------------------------------------------------------- happy path
def test_happy_path_records_checks_and_console(ctx):
    run_qualify(ctx)
    assert qualify_passed(ctx.db, ctx.g) == (True, [])
    lr = ctx.db.latest_runs()
    pc = det(ctx, "L37", "population-check")
    assert pc["verdict"] == "green" and pc["failed_rules"] == [] and pc["profile"] == "f4-compute-tp"
    assert pc["headline"] and pc["rows"][0] == ["profile", "f4-compute-tp"]
    for name in shipchecks.SHIP_CHECKS:
        r = lr[("L37", name)]
        assert r["verdict"] == "pass" and r["detail"]["checker"].startswith("rackpost ")
        rel = r["artifacts"]["check"]
        assert rel == f"artifacts/L37/fake371/{name}/check.txt"
        text = (ctx.dir / rel).read_text()
        assert text.startswith(f"check: {name}\nchecked by: rackpost ")
    assert det(ctx, "L37", "smbios-serial")["fru0_serials"] == ["FAKE371"]
    con = lr[("L37", "console")]
    assert con["verdict"] == "pass" and con["detail"]["bytes"] > 0
    assert con["artifacts"]["sol"].startswith("artifacts/L37/q") and \
        con["artifacts"]["sol"].endswith("/console/sol.txt")
    # the relay's file from its mark on (the fake relay's canned console after the header)
    assert (ctx.dir / con["artifacts"]["sol"]).read_text().startswith(
        "=== flax-post mark rackpost-qualify ===\n[SOL ")
    calls = ctx.rungs.calls
    i_start = calls.index(("sol_start", "L37"))
    i_boot = next(i for i, c in enumerate(calls) if c[:2] == ("boot_live", "L37"))
    i_mesh = next(i for i, c in enumerate(calls) if c[0] == "mesh")
    assert i_start < i_boot and calls.index(("sol_stop", "L37")) > i_mesh
    assert device_verdict(lr, ctx.g.device("L37")) == "pass"


# ---------------------------------------------------------------- population-check
def test_blocked_dimm_is_red_never_gates(ctx, profiles):
    (profiles / "blocklist-parts.json").write_text(json.dumps(
        {"dimms": [{"serial": "0000F00D", "reason": "cloned SPD"}]}))
    run_qualify(ctx, positions=["L37"])
    d = det(ctx, "L37", "population-check")
    assert v(ctx, "L37", "population-check") == "fail" and d["verdict"] == "red"
    assert d["blocked_dimms"][0]["serial"] == "0000F00D"
    assert any(r[0] == "BLOCKED" for r in d["rows"]) and d["extra"][0].startswith("blocked DIMM")
    assert "1 blocked DIMM(s)" in d["error"]
    assert all(r["ok"] for r in d["results"])          # the profile itself matched
    for rung in ("fio", "cpu-mem-stress", "console"):
        assert v(ctx, "L37", rung) == "pass", rung
    assert "L37" in qualify_passed(ctx.db, ctx.g)[1]


def test_unreadable_blocklist_fails_closed(ctx, profiles):
    (profiles / "blocklist-parts.json").write_text("{not json")
    run_qualify(ctx, positions=["L37"])
    assert "blocklist unreadable" in det(ctx, "L37", "population-check")["failed_rules"][0]


def _oob_fru(ctx, pos, text):
    """The BMC's out-of-band FRU 0 answer for `pos` (None = the BMC gives no FRU 0)."""
    from flax_rungs.identity import _fail, _tp_result
    base = ctx.rungs.tp_fru

    def tp_fru(ip, creds, **k):
        if ctx.rungs.ip_role.get(ip, (None,))[0] != pos:
            return base(ip, creds, **k)
        return _tp_result(text, "u", 17) if text is not None else \
            _fail("FRU 0 not read from " + ip, "Unable to establish IPMI v2 session (ciphers 17/3)")
    ctx.rungs.tp_fru = tp_fru


BLANK_FRU0 = ("FRU Device Description : Builtin FRU Device (ID 0)\n Product Name : Tioga Pass\n"
              " Product Serial :\n")


def test_blank_fru0_ship_serial_is_red(ctx):
    _oob_fru(ctx, "L37", BLANK_FRU0)
    _inv_patch(ctx, "L37", **{"ipmitool-fru": BLANK_FRU0})
    run_qualify(ctx, positions=["L37"])
    d = det(ctx, "L37", "population-check")
    assert d["no_ship_serial"] is True and d["verdict"] == "red"
    assert qualify_rules.NO_SHIP_RULE % "Product Serial" in d["failed_rules"]
    assert v(ctx, "L37", "smbios-serial") == "fail"        # the same hole, Post's 2nd judge
    assert det(ctx, "L37", "smbios-serial")["reason"] == "FRU 0 has no serial"


def test_population_fru0_is_out_of_band_never_the_in_band_capture(ctx):
    # ruling: FRU 0 from the BMC (Post's IPMI lane); the in-band capture is never used
    _oob_fru(ctx, "L37", BLANK_FRU0)                      # in-band capture still has SN
    run_qualify(ctx, positions=["L37"])
    d = det(ctx, "L37", "population-check")
    assert d["no_ship_serial"] is True and d["verdict"] == "red"
    rel = ctx.db.latest_runs()[("L37", "population-check")]["artifacts"]["fru0"]
    assert (ctx.dir / rel).read_text() == BLANK_FRU0


def test_failed_oob_fru_read_fails_population_no_backfill(ctx):
    _oob_fru(ctx, "L37", None)
    run_qualify(ctx, positions=["L37"])
    d = det(ctx, "L37", "population-check")
    # no FRU 0 from the BMC: nothing judged (unmeasured), never backfilled, never a pass
    assert v(ctx, "L37", "population-check") == "unmeasured"
    assert d["headline"].startswith("FRU 0 not read out of band: Unable to establish IPMI")
    assert v(ctx, "L37", "smbios-serial") == "pass"        # the in-band capture is fine
    assert "L37" in qualify_passed(ctx.db, ctx.g)[1]
    assert v(ctx, "L37", "fio") == "pass"                   # never gates


def test_absent_oob_fru0_fails_population(ctx):
    _oob_fru(ctx, "L37", "FRU Device Description : Builtin FRU Device (ID 0)\n Device not present\n")
    run_qualify(ctx, positions=["L37"])
    assert det(ctx, "L37", "population-check")["error"].startswith("FRU 0 not present")


def test_population_matches_posts_judge_on_the_same_inputs(ctx):
    run_qualify(ctx, positions=["L37"])
    lr = ctx.db.latest_runs()
    art = lambda n: (ctx.dir / lr[("L37", "inventory")]["artifacts"][n]).read_text()
    fru0 = (ctx.dir / lr[("L37", "population-check")]["artifacts"]["fru0"]).read_text()
    want = qualify_rules.population_check(art("macinv"), "f4-compute-tp", art("dimmsum"),
                                          qualify.baseboard_of(fru0))
    got = det(ctx, "L37", "population-check")
    assert {k: got[k] for k in want} == want


# ---------------------------------------------------------------- ship checks
def test_ship_check_failures_never_gate(ctx):
    _inv_patch(ctx, "R37", tpmstate="device=present\nowned=1 shEnable=1 ehEnable=1\n",
               dmidecode="System Information\n\tSerial Number: OTHER\n")
    run_qualify(ctx)
    assert det(ctx, "R37", "tpm-state")["reason"] == "owned (not cleared)"
    assert det(ctx, "R37", "smbios-serial")["reason"] == "SN FAKE373 in FRU 0, not in SMBIOS"
    assert det(ctx, "R37", "tpm-state")["error"] == "owned (not cleared)"
    for rung in ("fio", "sel-post", "iperf-mesh"):
        assert v(ctx, "R37", rung) == "pass", rung
    assert qualify_passed(ctx.db, ctx.g) == (False, ["R37"])
    ev = (ctx.dir / ctx.db.latest_runs()[("R37", "tpm-state")]["artifacts"]["check"]).read_text()
    assert "status: fail" in ev and "reason: owned (not cleared)" in ev


def test_missing_inventory_artifacts_fail_the_checks(ctx):
    _inv_patch(ctx, "L37", tpmstate=None, dmidecode=None)
    run_qualify(ctx, positions=["L37"])
    assert det(ctx, "L37", "tpm-state")["reason"] == "no tpmstate artifact"
    assert det(ctx, "L37", "smbios-serial")["reason"] == "no dmidecode artifact"


def test_launch_failure_skips_the_engine_checks(ctx):
    base = ctx.rungs.launch

    def launch(ip, u, p, **k):
        if ctx.rungs.pos_of[ip] == "C37" and k["stages"].startswith("sdr-pre"):
            return RungResult("fail", {"error": "agent launch rc=255"})
        return base(ip, u, p, **k)
    ctx.rungs.launch = launch
    run_qualify(ctx)
    assert det(ctx, "C37", "agent-reachable")["phase"] == "inventory"
    for rung in qualify.ENGINE_CHECKS:
        assert det(ctx, "C37", rung)["reason"] == "agent-reachable failed", rung
    # out of Qualify after inventory: its SOL is stopped then, not at the end
    calls = ctx.rungs.calls
    assert calls.index(("sol_stop", "C37")) < next(i for i, c in enumerate(calls)
                                                   if c[0] == "launch" and c[2] == "smart-health,fio")


# ---------------------------------------------------------------- the verdict
def test_agent_reported_skip_counts_as_post_does(ctx):
    base = ctx.rungs.poll

    def poll(ip, **k):
        out = base(ip, **k)
        if ctx.rungs.pos_of[ip] == "L37" and "fio" in out:
            out["fio"] = RungResult("skip", {"stage": "fio", "summary": {
                "reason": "no physical storage media"}})
        return out
    ctx.rungs.poll = poll
    run_qualify(ctx)
    assert v(ctx, "L37", "fio") == "skip" and qualify_passed(ctx.db, ctx.g) == (True, [])


def test_rackpost_skip_is_not_a_pass(ctx):
    run_qualify(ctx)
    rid = ctx.db.start_run("L37", "qualify", "fio")
    ctx.db.finish_run(rid, RungResult("skip", {"reason": "failed in phase inventory"}))
    lr = ctx.db.latest_runs()
    assert device_verdict(lr, ctx.g.device("L37")) is None
    assert qualify_passed(ctx.db, ctx.g) == (False, ["L37"])


# ---------------------------------------------------------------- console
def test_console_fail_fails_the_device_only(tmp_path, profiles):
    c = make_ctx(tmp_path, sol_fail={"R37"})
    run_qualify(c)
    assert v(c, "R37", "console") == "fail"
    assert det(c, "R37", "console")["error"].startswith("no sol capture (0 bytes")
    assert c.db.latest_runs()[("R37", "console")]["artifacts"] == {}
    assert v(c, "R37", "iperf-mesh") == "pass"
    assert qualify_passed(c.db, c.g) == (False, ["R37"])


def test_console_stop_raising_still_finishes_the_row(ctx):
    def boom(cap):
        raise RuntimeError("ipmitool vanished")
    ctx.rungs.sol_stop = boom
    run_qualify(ctx, positions=["L37"])
    r = ctx.db.latest_runs()[("L37", "console")]
    assert r["status"] == "done" and r["verdict"] == "fail"
    assert "ipmitool vanished" in r["detail"]["error"]


def test_console_stopped_when_a_phase_raises(ctx, monkeypatch):
    def bad(*a, **k):
        raise RuntimeError("network blew up")
    monkeypatch.setattr(qualify, "_network", bad)
    with pytest.raises(RuntimeError):
        run_qualify(ctx, positions=["L37"])
    assert ("sol_stop", "L37") in ctx.rungs.calls
    assert ctx.db.latest_runs()[("L37", "console")]["status"] == "done"


def test_never_booted_console_skipped(tmp_path, profiles):
    c = make_ctx(tmp_path, discover=False)
    run_qualify(c)
    assert not [x for x in c.rungs.calls if x[0] == "sol_start"]
    assert det(c, "L37", "console")["reason"] == "failed in phase boot"


# ---------------------------------------------------------------- old run names
def test_old_rung_names_display_under_posts(tmp_path, profiles):
    c = make_ctx(tmp_path)
    for rung in ("pop", "agent-launch"):
        c.db.finish_run(c.db.start_run("L37", "qualify", rung),
                        RungResult("pass", {"profile": "f4-compute-tp"}))
    rack = Rack(tmp_path, c.g)
    rack.db = c.db
    runs = {d["pos"]: d["runs"] for d in rack_state(rack)["devices"]}
    assert runs["L37"]["population-check"]["detail"]["profile"] == "f4-compute-tp"
    assert "agent-reachable" in runs["L37"] and "pop" not in runs["L37"]
    # a new run wins over the old name
    run_qualify(c, positions=["L37"])
    runs = {d["pos"]: d["runs"] for d in rack_state(rack)["devices"]}
    assert runs["L37"]["population-check"]["detail"]["verdict"] == "green"
    assert runs["L37"]["agent-reachable"]["detail"]["phase"] == "burn"
