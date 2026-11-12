"""observe/ladder: the pure per-slot rung machine (spec 2026-09-11 post-slot-ladder §4/§5)."""
import calendar

import pytest
import time

from flax_post.observe import ladder as L

SNAP = {"bmc_pinged": True, "power_on": "off", "host_leased": False, "verdict": None,
        "hold": False, "fw_flashing": False, "fw_gates": False, "allowed": True, "launch_at": None}


def snap(**kw):
    return dict(SNAP, **kw)


def run_to(rung, now=100.0):
    """Drive a fresh ladder to `rung` with passing evidence; return the slice."""
    lad, _ = L.advance(None, snap(), {}, now)                       # bmc-pinged -> power-on
    lad, acts = L.advance(lad, snap(), {}, now)                      # issues power-on
    assert "power-on" in acts
    if rung == "power-on":
        return lad
    lad, _ = L.advance(lad, snap(power_on="on"), {}, now + 5)        # -> tftp-seen
    steps = [("tftp-seen", {"tftp": now + 300.0}), ("ipxe-seen", {"ipxe": now + 310.0}),
             ("host-leased", {}), ("live-iso-seen", {"iso": now + 330.0}),
             ("host-pinged", {"ping": True}), ("host-ssh", {"ssh": True}),
             ("bmc-ready", {"bmcdata": {"board_mfg": "Wistron", "product": "Tioga Pass", "serial": "SN1"}}),
             ("fw-gates", {}), ("agent-reachable", {"agent": True}), ("qualify", {})]
    for name, ev in steps:
        if lad["rung"] == rung:
            return lad
        s = snap(power_on="on", host_leased=(name != "tftp-seen"), fw_gates=(name == "fw-gates"))
        lad, _ = L.advance(lad, s, ev, now + 340)
    return lad


def test_new_ladder_starts_at_bmc_pinged():
    lad = L.new_ladder(1.0)
    assert lad["rung"] == "bmc-pinged" and lad["fault"] is None
    assert lad["power_on_pending"] is False and lad["marks"] == {}


def test_bmc_pinged_passes_and_asks_fwd_probe():
    lad, acts = L.advance(None, snap(), {}, 10.0)
    assert lad["rung"] == "power-on" and acts == ["probe-fwd"]


def test_bmc_not_pinged_waits_forever_without_fault():
    lad, _ = L.advance(None, snap(bmc_pinged=False), {}, 10.0)
    lad, acts = L.advance(lad, snap(bmc_pinged=False), {}, 99999.0)
    assert lad["rung"] == "bmc-pinged" and lad["fault"] is None and acts == []


def test_power_on_issued_with_mark_and_claim_when_off_and_eligible():
    lad, _ = L.advance(None, snap(), {}, 10.0)
    lad, acts = L.advance(lad, snap(), {}, 11.0)
    assert acts == ["sol-mark:power-on", "claim", "power-on"]
    assert lad["power_on_pending"] is True and lad["power_on_at"] == 11.0
    assert lad["rung"] == "power-on"


def test_power_on_not_issued_when_verdict_hold_flashing_or_not_allowed():
    for block in ({"verdict": "pass"}, {"verdict": "fail"}, {"hold": True},
                  {"fw_flashing": True}, {"allowed": False}):
        lad, _ = L.advance(None, snap(**block), {}, 10.0)
        lad, acts = L.advance(lad, snap(**block), {}, 11.0)
        assert acts == [], block
        assert lad["power_on_pending"] is False and lad["fault"] is None


def test_power_on_cooldown_blocks_a_second_attempt():
    lad, _ = L.advance(None, snap(), {}, 10.0)
    lad, _ = L.advance(lad, snap(), {}, 11.0)
    lad["power_on_pending"] = False                       # simulate a cleared attempt
    lad, acts = L.advance(lad, snap(), {}, 11.0 + L.POWER_COOLDOWN_S - 1)
    assert acts == []
    lad, acts = L.advance(lad, snap(), {}, 11.0 + L.POWER_COOLDOWN_S + 1)
    assert "power-on" in acts


def test_power_on_seen_on_clears_pending_and_advances():
    lad = run_to("power-on")
    lad, acts = L.advance(lad, snap(power_on="on"), {}, 120.0)
    assert lad["rung"] == "tftp-seen" and lad["power_on_pending"] is False and acts == []


def test_power_on_budget_faults_and_unclaims():
    lad = run_to("power-on")
    lad, acts = L.advance(lad, snap(power_on="off"), {}, 100.0 + L.budget_s("power-on") + 1)
    assert lad["fault"] == {"rung": "power-on", "reason": "not on after 60s", "at": 161.0}
    assert lad["power_on_pending"] is False and acts == ["unclaim"]


def test_already_on_at_first_sight_skips_boot_markers():
    lad, _ = L.advance(None, snap(power_on="on"), {}, 10.0)
    lad, acts = L.advance(lad, snap(power_on="on"), {}, 11.0)
    assert lad["rung"] == "host-pinged" and lad["marks"]["skipped"] == "power already on"
    assert lad["power_on_at"] == 11.0 and acts == []


def test_unknown_power_reading_waits():
    lad, _ = L.advance(None, snap(power_on=None), {}, 10.0)
    lad, acts = L.advance(lad, snap(power_on=None), {}, 11.0)
    assert lad["rung"] == "power-on" and acts == []


def test_evidence_needed_per_rung():
    lad = run_to("tftp-seen"); assert L.evidence_needed(lad) == "tftp"
    lad = run_to("ipxe-seen"); assert L.evidence_needed(lad) == "ipxe"
    lad = run_to("live-iso-seen"); assert L.evidence_needed(lad) == "iso"
    lad = run_to("host-pinged"); assert L.evidence_needed(lad) == "ping"
    lad = run_to("host-ssh"); assert L.evidence_needed(lad) == "ssh"
    lad = run_to("bmc-ready"); assert L.evidence_needed(lad) == "bmcdata"
    lad = run_to("agent-reachable"); assert L.evidence_needed(lad) == "agent"
    lad = run_to("qualify"); assert L.evidence_needed(lad) == "agent"
    lad = run_to("fw-gates"); assert L.evidence_needed(lad) is None


def test_boot_markers_record_log_timestamps():
    lad = run_to("tftp-seen")
    lad, _ = L.advance(lad, snap(power_on="on"), {"tftp": 250.0}, 260.0)
    assert lad["rung"] == "ipxe-seen" and lad["marks"]["tftp"] == 250.0 and lad["since"] == 260.0


def test_tftp_budget_counts_from_power_on_at():
    lad = run_to("tftp-seen")                              # power_on_at == 100.0
    lad, acts = L.advance(lad, snap(power_on="on"), {"tftp": None}, 100.0 + 600 + 1)
    assert lad["fault"]["rung"] == "tftp-seen" and lad["fault"]["reason"] == "no tftp after 600s"
    assert acts == ["unclaim"]


def test_host_leased_comes_from_snapshot():
    lad = run_to("host-leased")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=False), {}, 400.0)
    assert lad["rung"] == "host-leased"
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), {}, 401.0)
    assert lad["rung"] == "live-iso-seen"


def test_host_ssh_pass_unclaims_and_probes_host_daemons():
    lad = run_to("host-ssh")
    lad, acts = L.advance(lad, snap(power_on="on", host_leased=True), {"ssh": True}, 500.0)
    assert lad["rung"] == "bmc-ready" and acts == ["unclaim", "probe-biosd", "probe-nicd"]
    assert lad["marks"]["ssh"] == 500.0


# ---- bmc-ready (ruling 2026-09-12): no agent, no BMC probe, until the BMC answers a data read ----
def test_bmc_ready_waits_while_the_bmc_pings_but_returns_no_data():
    lad = run_to("bmc-ready")
    lad, acts = L.advance(lad, snap(power_on="on", host_leased=True), {"bmcdata": None}, lad["since"] + 90)
    assert lad["rung"] == "bmc-ready" and lad["fault"] is None and acts == []


def test_bmc_ready_passes_on_fru_identity_records_it_and_reprobes_fwd():
    lad = run_to("bmc-ready")
    fru = {"board_mfg": "Wistron", "product": "Tioga Pass Single Side", "serial": "WTH1"}
    lad, acts = L.advance(lad, snap(power_on="on", host_leased=True), {"bmcdata": fru}, 700.0)
    assert lad["rung"] == "fw-gates" and acts == ["probe-fwd"]
    assert lad["marks"]["bmcready"] == 700.0 and lad["bmc_fru"] == fru




def test_agent_is_only_reachable_after_bmc_ready():
    assert L.RUNG_INDEX["bmc-ready"] < L.RUNG_INDEX["fw-gates"] < L.RUNG_INDEX["agent-reachable"]
    assert L.RUNG_INDEX["host-ssh"] < L.RUNG_INDEX["bmc-ready"]


def test_fw_gates_pass_moves_to_agent_and_polls():
    lad = run_to("fw-gates")
    lad, acts = L.advance(lad, snap(power_on="on", host_leased=True, fw_gates=True), {}, 600.0)
    assert lad["rung"] == "agent-reachable" and acts == ["poll-agent"]


def test_fw_gates_budget_faults():
    lad = run_to("fw-gates")
    lad, acts = L.advance(lad, snap(power_on="on", host_leased=True), {}, lad["since"] + 601)
    assert lad["fault"]["rung"] == "fw-gates" and "600s" in lad["fault"]["reason"]


def test_agent_reachable_budget_counts_from_launch_at():
    lad = run_to("agent-reachable")
    s = snap(power_on="on", host_leased=True, fw_gates=True, launch_at=700.0)
    lad, acts = L.advance(lad, s, {"agent": False}, 700.0 + 179)
    assert lad["fault"] is None and acts == ["poll-agent"]
    lad, acts = L.advance(lad, s, {"agent": False}, 700.0 + 181)
    assert lad["fault"]["rung"] == "agent-reachable"


def test_agent_reachable_passes_to_qualify_then_done_on_verdict():
    lad = run_to("agent-reachable")
    s = snap(power_on="on", host_leased=True, fw_gates=True, launch_at=700.0)
    lad, acts = L.advance(lad, s, {"agent": True}, 705.0)
    assert lad["rung"] == "qualify" and acts == ["poll-agent"]
    lad, acts = L.advance(lad, dict(s, verdict="pass"), {}, 900.0)
    assert lad["rung"] == "done" and acts == []


def test_fault_holds_across_iterations():
    lad = run_to("tftp-seen")
    lad, _ = L.advance(lad, snap(power_on="on"), {}, 100.0 + 601)
    for t in (800.0, 5000.0):
        lad2, acts = L.advance(lad, snap(power_on="on"), {"tftp": 799.0}, t)
        assert lad2["fault"] == lad["fault"] and lad2["rung"] == "tftp-seen" and acts == []


def test_hold_gesture_clears_a_fault():
    lad = run_to("tftp-seen")
    lad, _ = L.advance(lad, snap(power_on="on"), {}, 100.0 + 601)
    lad, _ = L.advance(lad, snap(power_on="on", hold=True), {}, 800.0)     # touched
    assert lad["fault"] and lad["hold_seen"] is True
    lad, acts = L.advance(lad, snap(power_on="on", hold=False), {}, 801.0)  # removed
    assert lad["fault"] is None and lad["rung"] == "bmc-pinged" and acts == ["unclaim"]


def test_human_reset_starts_at_tftp_with_operator_mark_and_claim():
    lad = L.human_reset(50.0)
    assert lad["rung"] == "tftp-seen" and lad["power_on_at"] == 50.0 and lad["human_power_on"]
    lad, acts = L.advance(lad, snap(power_on="on"), {"tftp": None}, 51.0)
    assert acts == ["sol-mark:operator", "claim", "pxe-cycle"] and "human_power_on" not in lad
    # the re-arm cycle is the engine's: stamped so the IPMI lane does not see a 2nd human
    assert lad["last_power_attempt"] == 51.0 == lad["power_on_at"]


def test_human_reset_rearm_is_once():
    lad, _ = L.advance(L.human_reset(50.0), snap(power_on="on"), {"tftp": None}, 51.0)
    lad, acts = L.advance(lad, snap(power_on="on"), {"tftp": None}, 52.0)
    assert "pxe-cycle" not in acts


@pytest.mark.parametrize("kw", [{"hold": True}, {"allowed": False}])
def test_human_reset_no_rearm_when_held_or_not_allowed(kw):
    # same predicate as the power-on rung: an operator's hold / an observe-only slot is
    # never power-cycled by the engine (review I2)
    lad, acts = L.advance(L.human_reset(50.0), snap(power_on="on", **kw), {"tftp": None}, 51.0)
    assert "pxe-cycle" not in acts and "last_power_attempt" not in lad


def test_human_reset_no_rearm_while_flashing():
    lad, acts = L.advance(L.human_reset(50.0), snap(power_on="on", fw_flashing=True),
                          {"tftp": None}, 51.0)
    assert "pxe-cycle" not in acts and "last_power_attempt" not in lad


def test_fault_helper_clears_pending():
    lad = run_to("power-on")
    lad = L.fault(lad, "power-on", "rejected: boom", 120.0)
    assert lad["fault"]["reason"] == "rejected: boom" and lad["power_on_pending"] is False


def test_intervals():
    assert L.interval_s(None) == 15
    assert L.interval_s({"rung": "tftp-seen", "fault": None}) == 2
    assert L.interval_s({"rung": "qualify", "fault": None}) == 10
    assert L.interval_s({"rung": "done", "fault": None}) == 15
    assert L.interval_s({"rung": "tftp-seen", "fault": {"rung": "tftp-seen"}}) == 15


def test_budget_env_override(monkeypatch):
    monkeypatch.setenv("FLAX_POST_LADDER_HOST_PINGED_S", "42")
    assert L.budget_s("host-pinged") == 42
    assert L.budget_s("bmc-pinged") is None


def test_stale_launch_at_does_not_start_the_agent_clock():
    """launch_at is never cleared, so a blade that reaches agent-reachable
    without a FRESH launch (the non-allowlisted case) carries one from hours
    ago; that must not fault the rung the instant it is entered."""
    lad = run_to("agent-reachable")
    since = lad["since"]
    budget = L.budget_s("agent-reachable")
    stale = snap(power_on="on", host_leased=True, fw_gates=True, launch_at=since - 5000)
    lad2, acts = L.advance(lad, stale, {"agent": False}, since + budget - 1)
    assert lad2["fault"] is None and acts == ["poll-agent"]
    lad3, _ = L.advance(lad, stale, {"agent": False}, since + budget + 1)
    assert lad3["fault"]["rung"] == "agent-reachable"      # the rung's own clock still runs


def test_launch_at_after_the_rung_started_moves_the_clock_forward():
    lad = run_to("agent-reachable")
    since = lad["since"]
    budget = L.budget_s("agent-reachable")
    fresh = snap(power_on="on", host_leased=True, fw_gates=True, launch_at=since + 100)
    lad2, _ = L.advance(lad, fresh, {"agent": False}, since + 100 + budget - 1)
    assert lad2["fault"] is None
    lad3, _ = L.advance(lad, fresh, {"agent": False}, since + 100 + budget + 1)
    assert lad3["fault"]["rung"] == "agent-reachable"


def test_already_on_with_a_verdict_short_circuits_to_done():
    """A latched blade that is powered on when the worker first sees it must
    not walk the rest of the ladder (ssh the host, fire probes, then fault at
    agent-reachable with no agent to reach)."""
    for v in ("pass", "fail"):
        lad, _ = L.advance(None, snap(power_on="on", verdict=v), {}, 10.0)
        lad, acts = L.advance(lad, snap(power_on="on", verdict=v), {}, 11.0)
        assert lad["rung"] == "done" and acts == [], v
        assert L.evidence_needed(lad) is None
        lad, acts = L.advance(lad, snap(power_on="on", verdict=v), {}, 12.0)
        assert lad["rung"] == "done" and acts == []


def test_agent_reachable_never_faults_when_launch_is_disabled():
    """A non-allowlisted powered-on blade has no agent and never will; it must
    not go red for that (et28b3, first deploy)."""
    lad = run_to("agent-reachable")
    s = snap(power_on="on", host_leased=True, fw_gates=True, allowed=False)
    lad, acts = L.advance(lad, s, {"agent": False}, lad["since"] + 100000)
    assert lad["fault"] is None and lad["rung"] == "agent-reachable" and acts == ["poll-agent"]
    # and it still passes the moment an agent appears
    lad, _ = L.advance(lad, s, {"agent": True}, lad["since"] + 100001)
    assert lad["rung"] == "qualify"


def test_agent_reachable_clock_restarts_when_the_allowlist_opens():
    """A slot parked at agent-reachable while not allowlisted must get the full
    budget once it is allowlisted, not fault on the stale rung start."""
    lad = run_to("agent-reachable")
    blocked = snap(power_on="on", host_leased=True, fw_gates=True, allowed=False)
    lad, _ = L.advance(lad, blocked, {"agent": False}, lad["since"] + 5000)   # long parked
    parked_at = lad["since"]
    allowed = snap(power_on="on", host_leased=True, fw_gates=True, allowed=True)
    lad, acts = L.advance(lad, allowed, {"agent": False}, parked_at + 2)      # allowlist opened
    assert lad["fault"] is None and acts == ["poll-agent"]
    lad, _ = L.advance(lad, allowed, {"agent": False}, parked_at + 181)
    assert lad["fault"]["rung"] == "agent-reachable"                          # budget still real


# ---- 2026-09-12 rack re-run findings ----
def test_power_on_unreadable_does_not_expire_the_60s_budget():
    """AMI/onetree BMCs drop off the network for >60 s right after a chassis
    power-on (et5b4, et6b3 2026-09-12: chassis WAS on, lane read None). The
    60 s budget is for a DEFINITE `off`; an unreadable BMC keeps waiting."""
    lad = run_to("power-on")
    lad, acts = L.advance(lad, snap(power_on=None), {}, 100.0 + L.budget_s("power-on") + 1)
    assert lad["fault"] is None and lad["rung"] == "power-on" and acts == []
    lad, acts = L.advance(lad, snap(power_on="on"), {}, 100.0 + 150)
    assert lad["rung"] == "tftp-seen" and lad["fault"] is None


def test_power_on_unreadable_has_its_own_longer_cap():
    lad = run_to("power-on")
    cap = L.budget_s("power-unreadable")
    assert cap > L.budget_s("power-on")
    lad, acts = L.advance(lad, snap(power_on=None), {}, 100.0 + cap + 1)
    assert lad["fault"]["rung"] == "power-on" and "unreadable" in lad["fault"]["reason"]
    assert acts == ["unclaim"]


def test_fw_gates_pass_freezes_the_firmware_steps_into_the_ladder():
    """The gate passing IS the firmware stage completing: freeze the Firmware
    step map here so a later daemon write (fwd's `unreachable` during the
    post-power-on BMC blackout, et28b4 2026-09-12) cannot regress it."""
    lad = run_to("fw-gates")
    fw = {"bmc-checked": "done", "bmc-updated": "done", "bios-checked": "done",
          "bios-updated": "done", "mlx-checked": "done", "mlx-updated": "done"}
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True, fw_gates=True, fw_steps=fw), {}, 600.0)
    assert lad["rung"] == "agent-reachable" and lad["fw_steps"] == fw
    # a later step with a regressed snapshot does not touch the frozen copy
    worse = dict(fw, **{"bmc-checked": "cur", "bmc-updated": "pending"})
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True, fw_gates=False, fw_steps=worse),
                       {"agent": True}, 601.0)
    assert lad["rung"] == "qualify" and lad["fw_steps"] == fw


def test_fw_gates_pass_without_steps_in_the_snapshot_freezes_nothing():
    lad = run_to("fw-gates")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True, fw_gates=True), {}, 600.0)
    assert "fw_steps" not in lad


# ---- post-verdict collection (ruling 2026-09-12) ----
def test_qualify_polls_as_evidence_and_passes_to_done_on_pass():
    lad = run_to("qualify")
    s = snap(power_on="on", host_leased=True, fw_gates=True)
    lad, acts = L.advance(lad, s, {"agent": True}, 1000.0)
    assert lad["rung"] == "qualify" and acts == []                 # polled as evidence, nothing to act
    lad, acts = L.advance(lad, dict(s, verdict="pass"), {"agent": True}, 1001.0)
    assert lad["rung"] == "done"


def test_fail_verdict_keeps_collecting_until_the_battery_is_terminal():
    lad = run_to("qualify")
    s = snap(power_on="on", host_leased=True, fw_gates=True, verdict="fail")
    lad, _ = L.advance(lad, s, {"agent": True}, 1000.0)
    assert lad["rung"] == "qualify"                                # battery still running
    lad, _ = L.advance(lad, dict(s, battery_done=True), {"agent": True}, 1010.0)
    assert lad["rung"] == "done" and "collection" not in lad


def test_fail_verdict_collection_is_cut_when_the_agent_is_gone_for_the_grace_period():
    lad = run_to("qualify")
    s = snap(power_on="on", host_leased=True, fw_gates=True, verdict="fail")
    lad, _ = L.advance(lad, s, {"agent": False}, 1000.0)
    assert lad["rung"] == "qualify" and lad["agent_lost_at"] == 1000.0
    lad, _ = L.advance(lad, s, {"agent": True}, 1030.0)             # a hiccup, the agent is back
    assert "agent_lost_at" not in lad
    lad, _ = L.advance(lad, s, {"agent": False}, 1040.0)
    lad, _ = L.advance(lad, s, {"agent": False}, 1040.0 + L.POST_VERDICT_AGENT_GRACE_S + 1)
    assert lad["rung"] == "done" and "agent unreachable" in lad["collection"]


# ---- boot proof for a blade already on at first sight (2026-09-14) ----
# Timelines are bang-gouda's dnsmasq/nginx logs of 2026-09-14 (UTC). Kernel boot
# times of powered-on hosts were measured the same day (bang clock - uptime)
# and always fell 51-56 s after the iPXE fetch and 17-21 s before the ISO fetch.
def _t(hms):
    return float(calendar.timegm(time.strptime("2026-09-14 " + hms, "%Y-%m-%d %H:%M:%S")))


def at_host_ssh_already_on(born, look):
    lad, _ = L.advance(None, snap(power_on="on"), {}, born)                  # born; bmc-pinged -> power-on
    lad, _ = L.advance(lad, snap(power_on="on"), {}, look)                   # already on -> host-pinged
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), {"ping": True}, look + 2)
    assert lad["rung"] == "host-ssh" and lad["marks"]["skipped"] == "power already on"
    return lad


def boot_ev(tftp, ipxe, iso, kernel, ok=True):
    return {"ssh-boot": {"ok": ok, "kernel_boot": _t(kernel) if kernel else None,
                         "tftp": [_t(x) for x in tftp], "ipxe": [_t(x) for x in ipxe],
                         "iso": [_t(x) for x in iso]}}


def _markers(lad):
    return {k: lad["marks"].get(k) for k in ("tftp", "ipxe", "iso")}


def test_already_on_ladder_asks_for_boot_proof_at_host_ssh():
    lad = at_host_ssh_already_on(_t("07:16:42"), _t("07:31:21"))
    assert L.evidence_needed(lad) == "ssh-boot"


def test_watched_boot_keeps_plain_ssh_evidence():
    assert L.evidence_needed(run_to("host-ssh")) == "ssh"


def test_ladder_without_a_birth_time_keeps_plain_ssh_evidence():
    """Ladders written before born_at existed cannot bound the search: unknown, as before."""
    lad = at_host_ssh_already_on(_t("07:16:42"), _t("07:31:21"))
    lad.pop("born_at", None)
    assert L.evidence_needed(lad) == "ssh"


def test_boot_proof_fills_the_markers_on_the_et10b2_timeline():
    lad = at_host_ssh_already_on(_t("07:16:42"), _t("07:31:21"))
    ev = boot_ev(["07:20:03"], ["07:21:11"], ["07:22:24"], "07:22:06")
    lad, acts = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("07:31:26"))
    assert lad["rung"] == "bmc-ready" and acts == ["unclaim", "probe-biosd", "probe-nicd"]
    assert _markers(lad) == {"tftp": _t("07:20:03"), "ipxe": _t("07:21:11"), "iso": _t("07:22:24")}
    assert lad["marks"]["kernel"] == _t("07:22:06") and lad["marks"]["ssh"] == _t("07:31:26")


def test_boot_proof_credits_the_running_boot_not_an_earlier_one():
    """et24b2 booted twice in one row; the running kernel booted at 08:46:03."""
    lad = at_host_ssh_already_on(_t("06:01:21"), _t("08:40:39"))
    ev = boot_ev(["06:50:52", "08:44:01"], ["06:52:00", "08:45:08"], ["06:53:10", "08:46:20"], "08:46:03")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("08:47:13"))
    assert _markers(lad) == {"tftp": _t("08:44:01"), "ipxe": _t("08:45:08"), "iso": _t("08:46:20")}


def test_boot_proof_never_credits_an_earlier_boot_to_the_running_kernel():
    """The running boot's lines are gone (rotation, read cap); only the earlier boot remains."""
    lad = at_host_ssh_already_on(_t("06:01:21"), _t("08:40:39"))
    ev = boot_ev(["06:50:52"], ["06:52:00"], ["06:53:10"], "08:46:03")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("08:47:13"))
    assert lad["rung"] == "bmc-ready"
    assert _markers(lad) == {"tftp": None, "ipxe": None, "iso": None}


def test_boot_proof_rejects_an_iso_fetch_too_long_after_ipxe():
    """et26b3: iPXE at 09:20:36, next ISO fetch 1922 s later."""
    lad = at_host_ssh_already_on(_t("06:19:54"), _t("09:50:00"))
    ev = boot_ev(["06:28:44", "08:59:13", "09:19:28"], ["06:29:52", "09:20:36"],
                 ["06:31:04", "09:52:38"], "09:52:20")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("09:53:30"))
    assert _markers(lad) == {"tftp": None, "ipxe": None, "iso": None}


def test_boot_proof_rejects_a_tftp_too_long_before_ipxe():
    """et26b3: a tftp at 08:59:13 never followed by iPXE."""
    lad = at_host_ssh_already_on(_t("06:19:54"), _t("09:20:00"))
    ev = boot_ev(["08:59:13"], ["09:20:36"], ["09:21:47"], "09:21:31")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("09:22:30"))
    assert _markers(lad) == {"tftp": None, "ipxe": None, "iso": None}


def test_boot_proof_tftp_leg_follows_the_ipxe_seen_budget(monkeypatch):
    """et25b4 2026-10-13: the BIOS posted its memory collection before the NBP,
    iPXE fetched its script 211 s after tftp. The watched ladder allows that
    (ipxe-seen budget 300 s on the bangs), so the proof must too."""
    monkeypatch.setenv("FLAX_POST_LADDER_IPXE_SEEN_S", "300")
    lad = at_host_ssh_already_on(_t("13:34:38"), _t("13:36:02"))
    ev = boot_ev(["13:36:21"], ["13:39:52"], ["13:41:05"], "13:40:45")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("13:41:58"))
    assert _markers(lad) == {"tftp": _t("13:36:21"), "ipxe": _t("13:39:52"), "iso": _t("13:41:05")}
    assert lad["marks"]["kernel"] == _t("13:40:45")


def test_boot_proof_tftp_leg_is_still_bounded_by_the_ipxe_seen_budget(monkeypatch):
    monkeypatch.setenv("FLAX_POST_LADDER_IPXE_SEEN_S", "300")
    lad = at_host_ssh_already_on(_t("13:30:00"), _t("13:31:00"))
    ev = boot_ev(["13:34:51"], ["13:39:52"], ["13:41:05"], "13:40:45")     # 301 s
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("13:41:58"))
    assert _markers(lad) == {"tftp": None, "ipxe": None, "iso": None}


def test_boot_proof_iso_leg_does_not_follow_the_ipxe_seen_budget(monkeypatch):
    monkeypatch.setenv("FLAX_POST_LADDER_IPXE_SEEN_S", "300")
    lad = at_host_ssh_already_on(_t("13:34:38"), _t("13:36:02"))
    ev = boot_ev(["13:38:45"], ["13:39:52"], ["13:42:05"], "13:41:45")     # ISO 133 s after iPXE
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("13:42:58"))
    assert _markers(lad) == {"tftp": None, "ipxe": None, "iso": None}


def test_boot_proof_ignores_lines_from_before_the_ladder_was_born():
    lad = at_host_ssh_already_on(_t("07:25:00"), _t("07:31:21"))
    ev = boot_ev(["07:20:03"], ["07:21:11"], ["07:22:24"], "07:22:06")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("07:31:26"))
    assert _markers(lad) == {"tftp": None, "ipxe": None, "iso": None}


def test_boot_proof_without_a_kernel_boot_time_passes_ssh_but_proves_nothing():
    lad = at_host_ssh_already_on(_t("07:16:42"), _t("07:31:21"))
    ev = boot_ev(["07:20:03"], ["07:21:11"], ["07:22:24"], None)
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("07:31:26"))
    assert lad["rung"] == "bmc-ready"
    assert _markers(lad) == {"tftp": None, "ipxe": None, "iso": None} and "kernel" not in lad["marks"]


def test_failed_ssh_boot_evidence_does_not_pass_host_ssh():
    lad = at_host_ssh_already_on(_t("07:16:42"), _t("07:31:21"))
    ev = boot_ev(["07:20:03"], ["07:21:11"], ["07:22:24"], "07:22:06", ok=False)
    lad, acts = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("07:31:26"))
    assert lad["rung"] == "host-ssh" and acts == [] and "ssh" not in lad["marks"]
    assert _markers(lad) == {"tftp": None, "ipxe": None, "iso": None}


# ---- occupant reset + bmc-ready retry (spec 2026-09-14-post-occupant-reset §4/§6) ----
def test_occupant_reset_is_a_fresh_ladder_born_when_the_old_blade_last_answered():
    lad = L.occupant_reset(40.0, 100.0)
    assert lad["rung"] == "bmc-pinged" and lad["since"] == 100.0 and lad["born_at"] == 40.0
    assert lad["marks"] == {} and lad["fault"] is None and "human_power_on" not in lad


def test_occupant_reset_without_a_usable_birth_is_born_now():
    assert L.occupant_reset(None, 100.0)["born_at"] == 100.0
    assert L.occupant_reset(200.0, 100.0)["born_at"] == 100.0      # never in the future


def test_occupant_reset_credits_a_boot_from_before_the_new_bmc_answered():
    """et10b4 2026-09-14: the new blade PXE-booted at 13:16, its BMC answered at 13:30."""
    lad = L.occupant_reset(_t("12:48:41"), _t("13:30:40"))
    lad, _ = L.advance(lad, snap(power_on="on"), {}, _t("13:30:41"))          # bmc-pinged -> power-on
    lad, _ = L.advance(lad, snap(power_on="on"), {}, _t("13:30:43"))          # already on -> host-pinged
    assert lad["rung"] == "host-pinged" and lad["marks"]["skipped"] == "power already on"
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), {"ping": True}, _t("13:30:45"))
    assert L.evidence_needed(lad) == "ssh-boot"
    ev = boot_ev(["13:16:06"], ["13:17:13"], ["13:18:25"], "13:18:08")
    lad, _ = L.advance(lad, snap(power_on="on", host_leased=True), ev, _t("13:30:47"))
    assert lad["rung"] == "bmc-ready"
    assert _markers(lad) == {"tftp": _t("13:16:06"), "ipxe": _t("13:17:13"), "iso": _t("13:18:25")}


def _no_bmc_data(lad, now):
    return L.advance(lad, snap(power_on="on", host_leased=True), {"bmcdata": None}, now)


def test_bmc_ready_first_expiry_restarts_the_wait_instead_of_faulting():
    lad = run_to("bmc-ready")
    t = lad["since"] + L.budget_s("bmc-ready") + 1
    lad, acts = _no_bmc_data(lad, t)
    assert lad["fault"] is None and lad["rung"] == "bmc-ready" and acts == []
    assert lad["since"] == t and len(lad["bmc_ready_attempts"]) == 1
    assert lad["bmc_ready_attempts"][0]["at"] == t


def test_bmc_ready_retries_with_600_720_840_then_faults():
    lad = run_to("bmc-ready")
    t = lad["since"]
    for budget in (L.budget_s("bmc-ready"), 600, 720):
        lad, _ = _no_bmc_data(lad, t + budget - 1)                   # inside this attempt's budget
        assert lad["fault"] is None and lad["since"] == t
        t = t + budget + 1
        lad, _ = _no_bmc_data(lad, t)                                # expired: restart
        assert lad["fault"] is None and lad["since"] == t
    lad, _ = _no_bmc_data(lad, t + 839)
    assert lad["fault"] is None
    lad, _ = _no_bmc_data(lad, t + 841)
    assert lad["fault"]["rung"] == "bmc-ready"
    assert "data reads after 4 attempts" in lad["fault"]["reason"]
    assert len(lad["bmc_ready_attempts"]) == 3


def test_bmc_data_during_a_retry_passes_to_fw_gates_and_keeps_the_attempts():
    lad = run_to("bmc-ready")
    lad, _ = _no_bmc_data(lad, lad["since"] + L.budget_s("bmc-ready") + 1)
    fru = {"board_mfg": "Wistron", "product": "Tioga Pass", "serial": "WTH1"}
    lad, acts = L.advance(lad, snap(power_on="on", host_leased=True), {"bmcdata": fru}, lad["since"] + 30)
    assert lad["rung"] == "fw-gates" and acts == ["probe-fwd"] and len(lad["bmc_ready_attempts"]) == 1


def test_bmc_ready_retry_budgets_env_override(monkeypatch):
    from flax_post import blades
    monkeypatch.setenv("FLAX_POST_LADDER_BMC_READY_RETRIES_S", "30,40")
    assert blades.bmc_ready_retry_budgets() == (30, 40)
    assert blades.bmc_ready_budget_s(0) == L.budget_s("bmc-ready")
    assert blades.bmc_ready_budget_s(1) == 30 and blades.bmc_ready_budget_s(2) == 40


def test_bmc_ready_retry_budgets_ignore_a_blank_or_invalid_env(monkeypatch):
    from flax_post import blades
    for bad in (" ", ",", "abc", "600,x"):
        monkeypatch.setenv("FLAX_POST_LADDER_BMC_READY_RETRIES_S", bad)
        assert blades.bmc_ready_retry_budgets() == blades.BMC_READY_RETRY_BUDGETS_S
        assert blades.bmc_ready_budget_s(3) == 840


from flax_post import blades as _blades

_ABSENT = {"state": "absent", "reason": "Device not present (Request data field length limit exceeded)",
           "serial": None, "board_mfg": None, "board_product": None, "addons": ["HPE Eth 10/25Gb"]}
_NO_SERIAL = {"state": "no_serial", "reason": None, "serial": None, "serial_field": "Product Serial",
              "board_mfg": "Wistron", "board_product": "Tioga Pass Single Side", "family": "tiogapass", "addons": []}
_S = dict(power_on="on", host_leased=True)


def _expire(lad, evidence):
    """Advance past the current bmc-ready budget with `evidence`."""
    now = lad["since"] + _blades.bmc_ready_budget_s(len(lad.get("bmc_ready_attempts") or [])) + 1
    return L.advance(lad, snap(**_S), {"bmcdata": evidence}, now)[0]


def test_bmc_ready_fru0_absent_is_not_a_pass_and_is_kept_for_the_modal():
    lad = run_to("bmc-ready")
    lad, acts = L.advance(lad, snap(**_S), {"bmcdata": _ABSENT}, lad["since"] + 5)
    assert lad["rung"] == "bmc-ready" and acts == [] and not lad.get("fault")
    assert lad["bmc_fru"]["state"] == "absent" and "bmcready" not in lad["marks"]


def test_bmc_ready_fru0_absent_waits_every_retry_then_faults_with_the_reason():
    lad = run_to("bmc-ready")
    for _ in range(len(_blades.bmc_ready_retry_budgets()) + 1):
        lad = _expire(lad, _ABSENT)
    assert lad["fault"]["rung"] == "bmc-ready"
    n = len(_blades.bmc_ready_retry_budgets()) + 1
    assert lad["fault"]["reason"].startswith("FRU 0 not present after %d attempts (" % n)
    assert lad["fault"]["reason"].endswith("): Device not present (Request data field length limit exceeded)")


def test_bmc_ready_not_answering_after_absent_keeps_the_old_fault_text():
    lad = run_to("bmc-ready")
    lad, _ = L.advance(lad, snap(**_S), {"bmcdata": _ABSENT}, lad["since"] + 5)
    for _ in range(len(_blades.bmc_ready_retry_budgets()) + 1):
        lad = _expire(lad, None)
    assert "bmc_fru" not in lad
    assert lad["fault"]["reason"].startswith("BMC not answering data reads after ")


def test_bmc_ready_no_serial_passes_and_stores_the_evidence():
    lad = run_to("bmc-ready")
    lad, acts = L.advance(lad, snap(**_S), {"bmcdata": _NO_SERIAL}, lad["since"] + 5)
    assert lad["rung"] == "fw-gates" and "probe-fwd" in acts
    assert lad["bmc_fru"] == _NO_SERIAL


def test_bmc_ready_absent_then_ok_passes():
    lad = run_to("bmc-ready")
    lad, _ = L.advance(lad, snap(**_S), {"bmcdata": _ABSENT}, lad["since"] + 5)
    ok = dict(_NO_SERIAL, state="ok", serial="WTH1")
    lad, _ = L.advance(lad, snap(**_S), {"bmcdata": ok}, lad["since"] + 10)
    assert lad["rung"] == "fw-gates" and lad["bmc_fru"]["serial"] == "WTH1"
