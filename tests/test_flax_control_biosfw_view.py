# tests/test_flax_control_biosfw_view.py
"""Tests for flax_control.biosfw_view — read-only triage BIOS-FW fleet view.

All tests use tmp_path + monkeypatch FLAX_CONFIG_DIR; no /etc/flax access.
Imports are function-local (see test_flax_control_bmcfw_view.py) so the
module is only imported once FLAX_CONFIG_DIR is monkeypatchable per test.
"""
import json
import pytest


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from flax_control import biosfw_view
    monkeypatch.setattr(biosfw_view, "FLAX_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_missing_store_is_empty(cfg):
    from flax_control import biosfw_view
    assert biosfw_view.read_store() == {}


def test_malformed_store_is_empty(cfg):
    from flax_control import biosfw_view
    (cfg / "biosfw.json").write_text("{not json")
    assert biosfw_view.read_store() == {}


def test_reads_rows(cfg):
    from flax_control import biosfw_view
    (cfg / "biosfw.json").write_text(json.dumps({
        "et10b2": {"port": "et10b2", "phase": "blocked",
                   "current_version": "TPC_P26C", "target_version": "TPC_P26F"}}))
    assert biosfw_view.read_store()["et10b2"]["phase"] == "blocked"


def test_phase_only_pills(cfg):
    from flax_control import biosfw_view
    assert biosfw_view.pill_for("up_to_date") == "ok"
    assert biosfw_view.pill_for("done") == "ok"
    assert biosfw_view.pill_for("fault") == "fail"
    assert biosfw_view.pill_for("flashing") == "inprogress"
    assert biosfw_view.pill_for("powering_off") == "inprogress"
    assert biosfw_view.pill_for("verifying") == "inprogress"
    assert biosfw_view.pill_for("unknown") == "neutral"


def test_confirmed_is_the_green_that_means_the_node_ran_this_bios(cfg):
    # The only state proving the node BOOTED AND RAN the BIOS. Before this it
    # had no pill at all and fell through to "neutral" -- a confirmed-good node
    # rendered grey while its blocked neighbours rendered amber.
    from flax_control import biosfw_view
    assert biosfw_view.pill_for("confirmed") == "ok"


def test_needs_power_cycle_is_red_because_no_retry_can_fix_it(cfg):
    from flax_control import biosfw_view
    assert biosfw_view.pill_for("needs_power_cycle") == "fail"


def test_blocked_alone_is_no_longer_interesting(cfg):
    # It is the DEFAULT phase of a healthy node that has not reported in band.
    # Painting it amber turned the whole page amber for a healthy fleet.
    from flax_control import biosfw_view
    assert biosfw_view.pill_for("blocked") == "neutral"


def test_a_blocked_row_with_a_known_delta_is_still_amber(cfg):
    # The case the page exists for: a real delta the staging gate is holding.
    from flax_control import biosfw_view
    assert biosfw_view.pill_for_row({"phase": "blocked",
                                     "current_version": "TPC_P26C",
                                     "target_version": "TPC_P26F"}) == "warn"


def test_a_blocked_row_with_no_report_is_not_amber(cfg):
    from flax_control import biosfw_view
    assert biosfw_view.pill_for_row({"phase": "blocked",
                                     "current_version": "",
                                     "target_version": "TPC_P26F"}) == "neutral"


def test_a_blocked_row_already_at_target_is_not_amber(cfg):
    # Same version on both sides is not a delta.
    from flax_control import biosfw_view
    assert biosfw_view.pill_for_row({"phase": "blocked",
                                     "current_version": "TPC_P26F",
                                     "target_version": "TPC_P26F"}) == "neutral"


def test_the_gate_column_distinguishes_no_report_from_held_delta(cfg):
    from flax_control import biosfw_view
    assert biosfw_view.gate_for({"phase": "blocked",
                                 "current_version": "TPC_P26C",
                                 "target_version": "TPC_P26F"}) == "blocked"
    assert biosfw_view.gate_for({"phase": "blocked",
                                 "target_version": "TPC_P26F"}) == "no report"
    assert biosfw_view.gate_for({"phase": "confirmed"}) == "cleared"
    assert biosfw_view.gate_for({"phase": "held"}) == "authorized"


def test_a_healthy_fleet_does_not_render_a_wall_of_amber(cfg):
    """THE regression. When the worker began keeping a row per managed
    candidate, every healthy node arrived as phase=blocked with no in-band
    version, and this page painted all of them amber while a genuinely
    confirmed-good node rendered grey -- the signal exactly inverted."""
    from flax_control import biosfw_view
    store = {"et10b%d" % i: {"port": "et10b%d" % i, "phase": "blocked",
                             "current_version": "",
                             "target_version": "TPC_P26F"}
             for i in range(1, 5)}
    store["et10b5"] = {"port": "et10b5", "phase": "confirmed",
                       "current_version": "TPC_P26F",
                       "target_version": "TPC_P26F"}
    rows = biosfw_view.fleet_rows(store)

    amber = [r for r in rows if r["phase_pill"] == "warn"]
    assert amber == [], "healthy candidates must not read as problems"

    confirmed = next(r for r in rows if r["port"] == "et10b5")
    assert confirmed["phase_pill"] == "ok", "the proven-good node is the green one"
    assert confirmed["gate"] == "cleared"
    assert all(r["gate"] == "no report" for r in rows if r["port"] != "et10b5")


def test_row_with_no_phase_reads_as_unknown(cfg):
    # Operator-visible badge text. "unheard-of" leaked in from a test probe
    # value; a phase-less row must read as a plain "unknown".
    from flax_control import biosfw_view
    rows = biosfw_view.fleet_rows({"et10b2": {"port": "et10b2"}})
    assert rows[0]["phase"] == "unknown"
    assert rows[0]["phase_pill"] == "neutral"


def test_targets_from_manifest(cfg):
    from flax_control import biosfw_view
    (cfg / "host-firmware-versions.json").write_text(json.dumps({
        "tiogapass-bios": {"match": {"product_name": ["(?i)tioga"]},
                           "target_version": "TPC_P26F",
                           "flash": {"artifact": "x"}, "auto": True}}))
    assert biosfw_view.targets()[0]["target_version"] == "TPC_P26F"


def test_targets_missing_manifest_is_empty(cfg):
    from flax_control import biosfw_view
    assert biosfw_view.targets() == []


def test_fleet_rows_joins_store_and_targets(cfg):
    """Not part of the brief's step-1 list, but exercised because the view
    needs a row-builder like bmcfw_view.fleet_rows to drive the template —
    one row per store entry, gate distinguished as blocked vs authorized."""
    from flax_control import biosfw_view
    (cfg / "host-firmware-versions.json").write_text(json.dumps({
        "tiogapass-bios": {"match": {"product_name": ["(?i)tioga"]},
                           "target_version": "TPC_P26F",
                           "flash": {"artifact": "x"}, "auto": True}}))
    (cfg / "biosfw.json").write_text(json.dumps({
        "et10b2": {"port": "et10b2", "phase": "blocked",
                   "current_version": "TPC_P26C", "target_version": "TPC_P26F"},
        "et10b3": {"port": "et10b3", "phase": "done",
                   "current_version": "TPC_P26F", "target_version": "TPC_P26F"},
    }))
    rows = biosfw_view.fleet_rows(biosfw_view.read_store())
    assert len(rows) == 2
    blocked = next(r for r in rows if r["port"] == "et10b2")
    assert blocked["phase"] == "blocked"
    assert blocked["gate"] == "blocked"
    assert blocked["phase_pill"] == "warn"
    done = next(r for r in rows if r["port"] == "et10b3")
    assert done["gate"] == "authorized"
    assert done["phase_pill"] == "ok"


# =======================================================================
# CHANGE 1: the per-port power-on hold, surfaced in the read-only view
# =======================================================================

def test_held_has_its_own_pill_and_is_neither_fault_nor_success(cfg):
    from flax_control import biosfw_view
    assert biosfw_view.pill_for("held") == "warn"
    assert biosfw_view.pill_for("held") != biosfw_view.pill_for("fault")
    assert biosfw_view.pill_for("held") != biosfw_view.pill_for("done")


def test_held_row_shows_the_operator_reason(cfg):
    from flax_control import biosfw_view
    rows = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "held",
        "hold_reason": "dbahi 2026-08-28: DIMM ECC storm, leave dark"}})
    r = rows[0]
    assert r["phase"] == "held"
    assert r["phase_pill"] == "warn"
    assert r["held"] is True
    assert "DIMM ECC storm" in r["note"]


def test_held_row_with_no_reason_still_says_it_is_pinned(cfg):
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {"port": "et10b2",
                                           "phase": "held"}})[0]
    assert r["held"] is True
    assert "pinned off" in r["note"]


def test_a_held_node_was_authorized_by_the_gate(cfg):
    # The gate DID let it through and the flash DID happen; what is withheld
    # is the power-on. The gate column must not lie about that.
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {"port": "et10b2",
                                           "phase": "held"}})[0]
    assert r["gate"] == "authorized"


def test_held_count_makes_pinned_nodes_visible_at_a_glance(cfg):
    from flax_control import biosfw_view
    rows = biosfw_view.fleet_rows({
        "et10b1": {"port": "et10b1", "phase": "held", "hold_reason": "x"},
        "et10b2": {"port": "et10b2", "phase": "done"},
        "et10b3": {"port": "et10b3", "phase": "held"},
    })
    assert biosfw_view.held_count(rows) == 2
    assert biosfw_view.held_count([]) == 0


def test_the_view_never_creates_anything_it_reads(cfg):
    # Reading a store that is not there must not conjure one -- flax_control
    # mounts /etc/flax read-only and a write attempt would be an error, not a
    # no-op.
    import os
    from flax_control import biosfw_view
    biosfw_view.read_store()
    biosfw_view.targets()
    assert os.listdir(str(cfg)) == []


# =======================================================================
# CHANGE 2: "still retrying" must be distinguishable from "gave up"
# =======================================================================

def test_a_retrying_fault_says_which_attempt(cfg):
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "fault", "fault_reason": "BMC unreachable",
        "attempts": 1, "max_attempts": 3, "gave_up": False}})[0]
    assert "retrying" in r["note"]
    assert "1" in r["note"] and "3" in r["note"]
    assert "BMC unreachable" in r["note"]      # original reason preserved
    assert r["phase_pill"] == "fail"


def test_a_terminal_fault_says_it_gave_up_and_keeps_the_reason(cfg):
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "fault", "fault_reason": "BMC unreachable",
        "attempts": 3, "max_attempts": 3, "gave_up": True}})[0]
    assert "gave up" in r["note"]
    assert "retrying" not in r["note"]
    assert "BMC unreachable" in r["note"]
    assert r["fault_reason"] == "BMC unreachable"


def test_a_fault_row_from_before_the_backoff_still_reads_sensibly(cfg):
    # A store file written by the previous release has no attempts/gave_up.
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "fault",
        "fault_reason": "could not confirm power off"}})[0]
    assert r["note"] == "could not confirm power off"


# =======================================================================
# CHANGE 3: a node left mid-flash across a restart is not silently skipped
# =======================================================================

def test_needs_attention_is_surfaced_and_outranks_everything(cfg):
    from flax_control import biosfw_view
    note = ("left in 'flashing' across a management-host restart: a BMC-side "
            "flash may still be running")
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "flashing", "needs_attention": note,
        "fault_reason": "something older"}})[0]
    assert r["needs_attention"] == note
    assert r["note"] == note
    assert biosfw_view.attention_count([r]) == 1


def test_rows_without_attention_do_not_claim_it(cfg):
    from flax_control import biosfw_view
    rows = biosfw_view.fleet_rows({"et10b2": {"port": "et10b2",
                                              "phase": "done"}})
    assert rows[0]["needs_attention"] == ""
    assert rows[0]["note"] == ""
    assert biosfw_view.attention_count(rows) == 0


# --- the rendered page -------------------------------------------------

def test_bios_fw_page_renders_held_and_retry_state(cfg, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from flax_control import app as app_mod

    (cfg / "biosfw.json").write_text(json.dumps({
        "et10b1": {"port": "et10b1", "phase": "held",
                   "current_version": "TPC_P26C", "target_version": "TPC_P26F",
                   "hold_reason": "dbahi: DIMM ECC storm, leave dark"},
        "et10b2": {"port": "et10b2", "phase": "fault",
                   "fault_reason": "BMC unreachable",
                   "attempts": 1, "max_attempts": 3, "gave_up": False},
        "et10b3": {"port": "et10b3", "phase": "flashing",
                   "needs_attention": "left in 'flashing' across a restart"},
    }))
    monkeypatch.setattr(app_mod.biosfw_view, "FLAX_CONFIG_DIR", str(cfg))
    body = TestClient(app_mod.app).get("/bios-fw").text
    assert "held" in body
    assert "DIMM ECC storm" in body            # the reason is TEXT on the page
    assert "pinned off" in body                # header summary
    assert "retrying" in body
    assert "need attention" in body


# --- a hold must be visible before the row ever reaches `held` ----------
#
# held_count counted rows in phase `held`. An operator who pinned a port that
# was currently fault/blocked/unknown -- or one with no row at all -- got no
# confirmation anywhere on the page that the hold had taken. The worker now
# stamps hold_set on every row it scans; the view has to show it.

def test_a_hold_on_a_faulted_row_is_visible(cfg):
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "fault", "fault_reason": "BMC unreachable",
        "attempts": 1, "max_attempts": 3, "gave_up": False,
        "hold_set": True, "hold_reason": "dbahi: pinned while I look"}})[0]
    assert r["hold_set"] is True
    assert r["held"] is False               # the phase is not falsified
    assert "hold set" in r["note"]
    assert "dbahi: pinned while I look" in r["note"]
    assert "retrying" in r["note"]          # and the fault state still shows
    assert biosfw_view.held_count([r]) == 1


def test_a_hold_on_a_blocked_row_is_visible(cfg):
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "blocked", "hold_set": True}})[0]
    assert r["hold_set"] is True
    assert "hold set" in r["note"]


def test_a_hold_on_a_row_with_no_phase_is_visible(cfg):
    # The worker creates a bare row for a held port it has never seen.
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "hold_set": True,
        "hold_reason": "dbahi: never seen this node"}})[0]
    assert r["phase"] == "unknown"
    assert r["hold_set"] is True
    assert "never seen this node" in r["note"]
    assert biosfw_view.held_count([r]) == 1


def test_held_count_counts_ports_with_holds_not_just_held_rows(cfg):
    from flax_control import biosfw_view
    rows = biosfw_view.fleet_rows({
        "et10b1": {"port": "et10b1", "phase": "held"},          # pinned
        "et10b2": {"port": "et10b2", "phase": "fault",
                   "hold_set": True},                           # pinned
        "et10b3": {"port": "et10b3", "phase": "done"},           # not
        "et10b4": {"port": "et10b4", "phase": "fault"},          # not
    })
    assert biosfw_view.held_count(rows) == 2


def test_a_row_with_no_hold_does_not_claim_one(cfg):
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {"port": "et10b2",
                                           "phase": "done"}})[0]
    assert r["hold_set"] is False
    assert "hold" not in r["note"]


def test_a_held_row_still_counts_without_an_explicit_hold_set(cfg):
    # Backwards compatible with rows written before the stamp existed.
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {"port": "et10b2",
                                           "phase": "held"}})[0]
    assert r["hold_set"] is True
    assert biosfw_view.held_count([r]) == 1


def test_needs_attention_and_a_hold_are_both_shown(cfg):
    # Startup recovery declines to power on a stranded node that is pinned.
    # The worker's needs_attention text already quotes the operator's note, so
    # the row must say both things ONCE -- repeating the note reads like two
    # separate holds.
    from flax_control import biosfw_view
    attention = ("stranded dark mid-sequence AND pinned by a per-port hold "
                 "(/etc/flax/bios-fw-hold/et10b2): NOT powered on by startup "
                 "recovery. Operator note: dbahi: ECC storm")
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "powering_on",
        "needs_attention": attention,
        "hold_set": True, "hold_reason": "dbahi: ECC storm",
        "stranded_by_hold": True}})[0]
    assert "stranded dark" in r["note"]
    assert "ECC storm" in r["note"]
    assert r["note"].count("ECC storm") == 1
    assert "hold set, power-on will be withheld" not in r["note"]
    assert biosfw_view.attention_count([r]) == 1
    assert biosfw_view.held_count([r]) == 1


def test_a_plain_hold_on_a_non_held_row_is_still_spelled_out(cfg):
    # The suppression above is only for stranded_by_hold rows, whose
    # needs_attention already carries the note.
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "fault", "fault_reason": "BMC unreachable",
        "hold_set": True, "hold_reason": "dbahi: ECC storm"}})[0]
    assert "hold set, power-on will be withheld" in r["note"]
    assert "ECC storm" in r["note"]


def test_page_shows_a_hold_on_a_faulted_node(cfg, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from flax_control import app as app_mod

    (cfg / "biosfw.json").write_text(json.dumps({
        "et10b2": {"port": "et10b2", "phase": "fault",
                   "fault_reason": "BMC unreachable",
                   "attempts": 1, "max_attempts": 3, "gave_up": False,
                   "hold_set": True,
                   "hold_reason": "dbahi: pinned while I look"}}))
    monkeypatch.setattr(app_mod.biosfw_view, "FLAX_CONFIG_DIR", str(cfg))
    body = TestClient(app_mod.app).get("/bios-fw").text
    assert "hold set" in body
    assert "pinned while I look" in body
    assert "1 pinned off" in body


def test_case_a_held_row_flagged_by_recovery_does_not_repeat_the_note(cfg):
    # Case A is phase="held" AND stranded_by_hold=True -- recover_stranded's
    # gate branch produces exactly that for a held row with a leftover
    # authorized gate entry. The suppression guard was on the hold_set branch
    # only, so this shape printed the operator's note twice.
    from flax_control import biosfw_view
    attention = ("stranded dark mid-sequence AND pinned by a per-port hold "
                 "(/etc/flax/bios-fw-hold/et10b2): NOT powered on by startup "
                 "recovery. Operator note: dbahi: ECC storm")
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "held", "needs_attention": attention,
        "hold_set": True, "hold_reason": "dbahi: ECC storm",
        "stranded_by_hold": True}})[0]
    assert r["note"].count("ECC storm") == 1
    assert "stranded dark" in r["note"]
    assert r["held"] is True
    assert biosfw_view.held_count([r]) == 1
    assert biosfw_view.attention_count([r]) == 1


def test_a_held_row_recovery_never_touched_still_shows_its_reason(cfg):
    # The suppression must not swallow the note on an ordinary held row.
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "held",
        "hold_set": True, "hold_reason": "dbahi: ECC storm"}})[0]
    assert "ECC storm" in r["note"]


def test_a_held_row_flagged_without_an_attention_text_still_shows_its_reason(cfg):
    # stranded_by_hold with no needs_attention should not silently drop the
    # note -- the guard is "already stated", not "stranded".
    from flax_control import biosfw_view
    r = biosfw_view.fleet_rows({"et10b2": {
        "port": "et10b2", "phase": "held", "stranded_by_hold": True,
        "hold_set": True, "hold_reason": "dbahi: ECC storm"}})[0]
    assert "ECC storm" in r["note"]
