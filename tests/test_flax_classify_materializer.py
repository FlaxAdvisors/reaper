"""Tests for flax_classify.materializer -- the shadow diff engine (Task 5).

Bulk of this file is PURE (no I/O): `plan_materialization(desired, actuals,
owners)` is computed from plain dicts. One DB-backed test at the bottom
covers `run_shadow`'s apply layer (write plan rows sharing a run id +
7-day retention); it SKIPS locally (no Postgres) and runs for real on gouda
at deploy, same convention as tests/test_flax_classify_desired_reservations.py.
"""
import os

import pytest

from flax_classify.materializer import parse_enforce_config, plan_materialization


# ── fixtures/helpers ─────────────────────────────────────────────────────────

MAC = "aa:bb:cc:00:11:22"
MAC2 = "aa:bb:cc:00:11:23"


def _desired(**overrides):
    d = dict(mac=MAC, owner_role="triage", kind="bmc",
             hostname="et6b1-bmc", ipv4="10.0.0.5", ipv6="fd00::5",
             vid=16, switch="rabbit-test", port="et6b1",
             attrs={}, generation=1, updated_at=None)
    d.update(overrides)
    return d


def _actual(**overrides):
    a = dict(mac=MAC, hostname="et6b1-bmc", ipv4="10.0.0.5", ipv6="fd00::5",
             vid=16, source="triage", has_classify=True)
    a.update(overrides)
    return a


def _apply_detail(d):
    """The JSON-safe apply subset plan_materialization embeds as
    detail["desired"]: EXACTLY the seven non-mac upsert kwargs. Never the
    raw desired row -- updated_at is a datetime, which psycopg's Jsonb
    (stock json.dumps) cannot serialize, so embedding it would abort the
    whole plan-row insert."""
    return {k: d.get(k) for k in
            ("switch", "port", "kind", "vid", "hostname", "ipv4", "ipv6")}


OWNERS = {"triage", "post"}

NO_SKIPS = {"unowned": 0, "unregistered_desired": 0, "multi_actual": 0}


def _skips(**kw):
    d = dict(NO_SKIPS)
    d.update(kw)
    return d


# ── converged / no-op ────────────────────────────────────────────────────────

def test_converged_yields_no_actions():
    actions, skipped = plan_materialization([_desired()], [_actual()], OWNERS)
    assert actions == []
    assert skipped == NO_SKIPS


def test_mac_case_is_normalized_on_both_sides():
    """Same device, differently-cased mac strings on each side -- must still
    compare as converged (self-review invariant)."""
    d = _desired(mac="AA:BB:CC:00:11:22")
    a = _actual(mac="aa:bb:cc:00:11:22")
    actions, skipped = plan_materialization([d], [a], OWNERS)
    assert actions == []
    assert skipped == NO_SKIPS


# ── missing actual ───────────────────────────────────────────────────────────

def test_missing_actual_upserts():
    d = _desired()
    actions, skipped = plan_materialization([d], [], OWNERS)
    assert actions == [("triage", "upsert", MAC,
                       {"reason": "missing", "desired": _apply_detail(d)})]
    assert skipped == NO_SKIPS


# ── per-field drift ──────────────────────────────────────────────────────────

def test_hostname_drift_upserts_with_field_named():
    d = _desired()
    actions, skipped = plan_materialization(
        [d], [_actual(hostname="stale-name")], OWNERS)
    assert actions == [("triage", "upsert", MAC,
                       {"fields": ["hostname"], "desired": _apply_detail(d)})]
    assert skipped == NO_SKIPS


def test_ipv4_drift_upserts_with_field_named():
    d = _desired()
    actions, skipped = plan_materialization(
        [d], [_actual(ipv4="10.0.0.99")], OWNERS)
    assert actions == [("triage", "upsert", MAC,
                       {"fields": ["ipv4"], "desired": _apply_detail(d)})]


def test_vid_drift_upserts_with_field_named():
    d = _desired()
    actions, skipped = plan_materialization(
        [d], [_actual(vid=99)], OWNERS)
    assert actions == [("triage", "upsert", MAC,
                       {"fields": ["vid"], "desired": _apply_detail(d)})]


def test_ipv6_drift_upserts_with_field_named():
    d = _desired()
    actions, skipped = plan_materialization(
        [d], [_actual(ipv6="fd00::99")], OWNERS)
    assert actions == [("triage", "upsert", MAC,
                       {"fields": ["ipv6"], "desired": _apply_detail(d)})]


def test_multi_field_drift_names_all_fields():
    actions, skipped = plan_materialization(
        [_desired()], [_actual(hostname="stale", vid=1)], OWNERS)
    assert len(actions) == 1
    owner, action, mac, detail = actions[0]
    assert (owner, action, mac) == ("triage", "upsert", MAC)
    assert set(detail["fields"]) == {"hostname", "vid"}


# ── owner's actual without any desired row -> delete ────────────────────────

def test_owned_actual_without_desired_deletes():
    actions, skipped = plan_materialization([], [_actual()], OWNERS)
    assert actions == [("triage", "delete", MAC, {})]
    assert skipped == NO_SKIPS


# ── owner mismatch on same mac -> purge_handoff ─────────────────────────────

def test_owner_mismatch_purge_handoff():
    d = _desired(owner_role="post")
    a = _actual(source="triage")
    actions, skipped = plan_materialization([d], [a], OWNERS)
    assert actions == [("post", "purge_handoff", MAC,
                       {"from": "triage", "to": "post", "desired": _apply_detail(d)})]
    assert skipped == NO_SKIPS


# ── action details must be JSON-serializable (Jsonb plan-row insert) ────────

def test_action_details_json_safe_with_real_datetime_and_attrs():
    """REGRESSION (Task 4 review, Critical): read_desired rows carry
    updated_at as a real Python datetime (and attrs as a dict). Embedding
    the RAW row into detail["desired"] made psycopg's Jsonb (stock
    json.dumps) raise TypeError at the plan-row executemany -- in default
    shadow mode that rolled back the whole plan-row+summary write on every
    cycle that planned an upsert/purge_handoff. The detail must embed ONLY
    the seven apply kwargs and json.dumps cleanly."""
    import datetime
    import json

    d = _desired(updated_at=datetime.datetime(2026, 7, 4, 12, 0, 0,
                                              tzinfo=datetime.timezone.utc),
                 attrs={"note": "operator", "n": 1}, generation=42)
    # upsert (missing), upsert (drift), and purge_handoff shapes all embed
    # the desired subset -- exercise all three.
    shapes = [
        plan_materialization([d], [], OWNERS),
        plan_materialization([d], [_actual(hostname="stale")], OWNERS),
        plan_materialization([dict(d, owner_role="post")],
                             [_actual(source="triage")], OWNERS),
    ]
    for actions, _skipped in shapes:
        assert len(actions) == 1
        detail = actions[0][3]
        json.dumps(detail)  # must not raise (TypeError pre-fix)
        assert set(detail["desired"]) == {
            "switch", "port", "kind", "vid", "hostname", "ipv4", "ipv6"}


# ── legacy-import / unknown-source actuals: never planned, safety-skip ──────

def test_legacy_import_actual_alone_is_never_planned():
    actions, skipped = plan_materialization(
        [], [_actual(source="legacy-import", has_classify=False)], OWNERS)
    assert actions == []
    assert skipped == NO_SKIPS


def test_unknown_source_actual_alone_is_never_planned():
    actions, skipped = plan_materialization(
        [], [_actual(source="some-other-service", has_classify=False)], OWNERS)
    assert actions == []
    assert skipped == NO_SKIPS


def test_desired_sharing_mac_with_legacy_import_actual_is_skipped_unowned():
    d = _desired()
    a = _actual(source="legacy-import", has_classify=False)
    actions, skipped = plan_materialization([d], [a], OWNERS)
    assert actions == [], "safety: never plan against an unowned actual"
    assert skipped == _skips(unowned=1)


def test_desired_sharing_mac_with_unknown_source_actual_is_skipped_unowned():
    d = _desired()
    a = _actual(source="some-other-service", has_classify=False)
    actions, skipped = plan_materialization([d], [a], OWNERS)
    assert actions == []
    assert skipped == _skips(unowned=1)


# ── untagged-with-classify legacy transition rule -> attributed "triage" ────

def test_untagged_with_classify_attributed_triage_and_deletes_if_no_desired():
    a = _actual(source=None, has_classify=True)
    actions, skipped = plan_materialization([], [a], OWNERS)
    assert actions == [("triage", "delete", MAC, {})]
    assert skipped == NO_SKIPS


def test_untagged_with_classify_converges_against_triage_desired():
    d = _desired(owner_role="triage")
    a = _actual(source=None, has_classify=True)
    actions, skipped = plan_materialization([d], [a], OWNERS)
    assert actions == []
    assert skipped == NO_SKIPS


def test_untagged_without_classify_is_never_planned():
    """source=None and no classify key: not a recognized legacy shape at all
    (neither post nor a pre-tag triage row) -- must be treated as unowned."""
    d = _desired()
    a = _actual(source=None, has_classify=False)
    actions, skipped = plan_materialization([d], [a], OWNERS)
    assert actions == []
    assert skipped == _skips(unowned=1)


# ── unregistered-owner desired rows: skip the mac entirely ──────────────────

def test_unregistered_desired_owner_blocks_delete_of_owned_actual():
    """Handoff-in-progress shape: desired moved to post, actual still triage,
    but post is NOT in the registry. The desired row must not become
    invisible -- planning a triage delete here would be phase-3 data loss.
    The whole mac is skipped (no action of any kind) and counted."""
    d = _desired(owner_role="post")
    a = _actual(source="triage")
    actions, skipped = plan_materialization([d], [a], {"triage"})
    assert actions == []
    assert skipped == _skips(unregistered_desired=1)


def test_unregistered_desired_owner_alone_is_skipped_and_counted():
    d = _desired(owner_role="some-future-role")
    actions, skipped = plan_materialization([d], [], OWNERS)
    assert actions == []
    assert skipped == _skips(unregistered_desired=1)


# ── duplicate-mac actuals: never collapse last-write-wins ────────────────────

def test_same_mac_two_vids_same_owner_is_skipped_multi_actual():
    """kea.hosts keys on (mac, type, subnet) so one mac CAN have rows in two
    subnets (vid-change dup). Silently keeping one row would make the plan
    nondeterministic (converged vs flapping depending on row order) -- the
    mac must be skipped and counted instead."""
    a1 = _actual(vid=16)
    a2 = _actual(vid=17)
    actions, skipped = plan_materialization([_desired()], [a1, a2], OWNERS)
    assert actions == []
    assert skipped == _skips(multi_actual=1)


def test_multi_actual_skip_is_order_independent():
    a1 = _actual(vid=16)
    a2 = _actual(vid=17)
    plan_fwd = plan_materialization([_desired()], [a1, a2], OWNERS)
    plan_rev = plan_materialization([_desired()], [a2, a1], OWNERS)
    assert plan_fwd == plan_rev == ([], _skips(multi_actual=1))


def test_multi_actual_without_desired_is_also_skipped_not_deleted():
    a1 = _actual(vid=16)
    a2 = _actual(vid=17)
    actions, skipped = plan_materialization([], [a1, a2], OWNERS)
    assert actions == []
    assert skipped == _skips(multi_actual=1)


def test_exact_duplicate_actual_rows_dedupe_to_one_not_skipped():
    """The ipv6_reservations LEFT JOIN can fan one underlying kea.hosts row
    out into several identical dicts -- exact duplicates must be deduped to
    one row FIRST (not counted as multi_actual), so the common v6-join case
    does not mask real drift."""
    a1 = _actual()
    a2 = _actual()
    actions, skipped = plan_materialization([_desired()], [a1, a2], OWNERS)
    assert actions == []
    assert skipped == NO_SKIPS


def test_exact_duplicate_actual_rows_still_detect_drift():
    d = _desired()
    a1 = _actual(hostname="stale-name")
    a2 = _actual(hostname="stale-name")
    actions, skipped = plan_materialization([d], [a1, a2], OWNERS)
    assert actions == [("triage", "upsert", MAC,
                       {"fields": ["hostname"], "desired": _apply_detail(d)})]
    assert skipped == NO_SKIPS


# ── empty owners -> empty plan (registry stand-down) ────────────────────────

def test_empty_owners_yields_empty_plan():
    actions, skipped = plan_materialization([_desired()], [_actual()], set())
    assert actions == []
    assert skipped == NO_SKIPS


def test_second_unrelated_mac_does_not_interfere():
    """Sanity: two independent macs each plan correctly in the same call."""
    d1 = _desired(mac=MAC)
    d2 = _desired(mac=MAC2, hostname="et6b2-bmc")
    a1 = _actual(mac=MAC)
    actions, skipped = plan_materialization([d1, d2], [a1], OWNERS)
    assert actions == [("triage", "upsert", MAC2,
                       {"reason": "missing", "desired": _apply_detail(d2)})]
    assert skipped == NO_SKIPS


# ── parse_enforce_config: two-key arming (phase 3a) ─────────────────────────

def test_parse_enforce_config_default_env_is_all_shadow():
    enforced, max_deletes = parse_enforce_config({})
    assert enforced == frozenset()
    assert max_deletes == 20


def test_parse_enforce_config_enforce_mode_with_roles_arms_them():
    enforced, max_deletes = parse_enforce_config(
        {"MATERIALIZER_MODE": "enforce", "MATERIALIZER_ENFORCE_ROLES": "post"})
    assert enforced == frozenset({"post"})
    assert max_deletes == 20


def test_parse_enforce_config_shadow_mode_is_kill_switch_over_roles():
    enforced, _ = parse_enforce_config(
        {"MATERIALIZER_MODE": "shadow", "MATERIALIZER_ENFORCE_ROLES": "post"})
    assert enforced == frozenset()


def test_parse_enforce_config_unsupported_mode_forces_shadow_and_logs(caplog):
    with caplog.at_level("ERROR"):
        enforced, _ = parse_enforce_config(
            {"MATERIALIZER_MODE": "garbage", "MATERIALIZER_ENFORCE_ROLES": "post"})
    assert enforced == frozenset()
    assert any("garbage" in rec.message for rec in caplog.records)


def test_parse_enforce_config_roles_are_comma_split_stripped_lowered():
    enforced, _ = parse_enforce_config(
        {"MATERIALIZER_MODE": "enforce",
         "MATERIALIZER_ENFORCE_ROLES": "post, TRIAGE"})
    assert enforced == frozenset({"post", "triage"})


def test_parse_enforce_config_max_deletes_non_integer_defaults_and_logs(caplog):
    with caplog.at_level("ERROR"):
        _, max_deletes = parse_enforce_config(
            {"MATERIALIZER_MAX_DELETES_PER_RUN": "abc"})
    assert max_deletes == 20
    assert any("abc" in rec.message for rec in caplog.records)


def test_parse_enforce_config_max_deletes_negative_defaults_and_logs(caplog):
    with caplog.at_level("ERROR"):
        _, max_deletes = parse_enforce_config(
            {"MATERIALIZER_MAX_DELETES_PER_RUN": "-5"})
    assert max_deletes == 20
    assert any("-5" in rec.message for rec in caplog.records)


def test_parse_enforce_config_max_deletes_valid_integer_parsed():
    _, max_deletes = parse_enforce_config(
        {"MATERIALIZER_MAX_DELETES_PER_RUN": "7"})
    assert max_deletes == 7


# ── run key: "<process-token>:<counter>", restart-proof ─────────────────────

def test_run_id_shape_and_monotonic_within_process():
    import re

    from flax_classify import materializer

    r1 = materializer._next_run_id()
    r2 = materializer._next_run_id()
    assert re.fullmatch(r"[0-9a-f]{8}:\d+", r1)
    assert r1 != r2
    tok1, n1 = r1.split(":")
    tok2, n2 = r2.split(":")
    assert tok1 == tok2 == materializer._RUN_TOKEN, \
        "one process lifetime shares one token"
    assert int(n2) == int(n1) + 1


def test_run_id_differs_across_simulated_restart(monkeypatch):
    """A restart resets the counter but mints a fresh token, so even the
    ADJACENT collision shape (prior lifetime's last counter == the new
    lifetime's first, nothing written between) yields distinct run keys --
    the viewer's equality-based grouping cannot merge two lifetimes."""
    from flax_classify import materializer

    r_before = materializer._next_run_id()
    # Simulate a restart: fresh process token, counter positioned so the
    # first post-restart run reuses the exact same counter integer.
    monkeypatch.setattr(materializer, "_RUN_TOKEN", "deadbeef")
    monkeypatch.setattr(materializer, "_run_counter",
                        [int(r_before.split(":")[1]) - 1])
    r_after = materializer._next_run_id()
    assert r_before.split(":")[1] == r_after.split(":")[1], \
        "test shape: same counter integer on both sides"
    assert r_before != r_after, "token must disambiguate across restarts"


# ── run_shadow always writes a summary marker row (mocked pool, no DB) ──────

def test_run_shadow_always_writes_summary_row_even_when_converged(monkeypatch):
    """A fully converged run (no planned actions) must still write exactly
    one 'summary' marker row -- this is what makes every run discoverable
    from the plan table alone (see materializer.py / shadow_view.py
    docstrings). Mocked pool: no DB needed, and this isolates the "even when
    planned==0" case that the DB test below can't reliably exercise against
    a live/populated gouda DB."""
    from unittest.mock import MagicMock

    from flax_classify import materializer

    monkeypatch.setattr(materializer, "read_desired", lambda pool: [])
    monkeypatch.setattr(materializer, "read_kea_actuals", lambda pool: [])
    monkeypatch.setattr(
        materializer, "plan_materialization",
        lambda desired, actuals, owners: (
            [], {"unowned": 0, "unregistered_desired": 0, "multi_actual": 0}))

    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn

    result = materializer.run_shadow(pool, {"triage"})

    assert result == {"planned": 0, "by_action": {},
                      "skipped": {"unowned": 0, "unregistered_desired": 0,
                                  "multi_actual": 0}}
    cursor.executemany.assert_not_called()  # no action rows -> no executemany
    assert cursor.execute.call_count == 1, "exactly the summary insert"
    sql, params = cursor.execute.call_args[0]
    assert "INSERT INTO materializer_plan" in sql
    assert params["owner_role"] == "-"
    assert params["action"] == "summary"
    assert params["mac"] == "-"
    detail = params["detail"].obj
    assert detail["planned"] == 0
    assert detail["by_action"] == {}
    assert detail["skipped"] == {"unowned": 0, "unregistered_desired": 0,
                                 "multi_actual": 0}
    assert "run" in detail


# ── Task 4: apply_actions dispatch (pure dispatch, monkeypatched writers) ───

def test_apply_actions_upsert_dispatches_to_upsert_kea_host(monkeypatch):
    from flax_classify import materializer

    calls = []
    monkeypatch.setattr(materializer, "upsert_kea_host",
                        lambda pool, **kw: calls.append(kw))
    d = _desired()
    actions = [("triage", "upsert", MAC, {"reason": "missing", "desired": _apply_detail(d)})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac={})

    assert calls == [{
        "switch": d["switch"], "port": d["port"], "mac": d["mac"],
        "kind": d["kind"], "vid": d["vid"], "ipv4_address": d["ipv4"],
        "hostname": d["hostname"], "ipv6_address": d["ipv6"],
        "source": "triage",
    }]
    assert result["applied"] == 1
    assert result["apply_errors"] == 0
    assert result["skipped_operator_note"] == 0
    assert result["breaker_tripped"] == []
    assert result["results"] == [
        {"applied": True, "apply_error": None,
         "skipped_operator_note": False, "breaker": False}]


def test_apply_actions_delete_dispatches_to_delete_hosts_for_mac(monkeypatch):
    from flax_classify import materializer

    calls = []
    monkeypatch.setattr(materializer, "delete_hosts_for_mac",
                        lambda pool, **kw: calls.append(kw) or 1)
    actions = [("triage", "delete", MAC, {})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac={MAC: {"operator_note": False}})

    assert calls == [{"mac": MAC, "source": "triage"}]
    assert result["applied"] == 1
    assert result["skipped_operator_note"] == 0
    # The helper's rowcount is recorded per action (audit residue).
    assert result["results"][0]["deleted_rows"] == 1


def test_apply_actions_purge_handoff_deletes_old_and_upserts_new(monkeypatch):
    from flax_classify import materializer

    delete_calls, upsert_calls = [], []
    monkeypatch.setattr(materializer, "delete_hosts_for_mac",
                        lambda pool, **kw: delete_calls.append(kw) or 1)
    monkeypatch.setattr(materializer, "upsert_kea_host",
                        lambda pool, **kw: upsert_calls.append(kw))
    d = _desired(owner_role="post")
    actions = [("post", "purge_handoff", MAC,
               {"from": "triage", "to": "post", "desired": _apply_detail(d)})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"post"}),
        max_deletes=20, actuals_by_mac={MAC: {"operator_note": False}})

    assert delete_calls == [{"mac": MAC, "source": "triage"}]
    assert upsert_calls == [{
        "switch": d["switch"], "port": d["port"], "mac": d["mac"],
        "kind": d["kind"], "vid": d["vid"], "ipv4_address": d["ipv4"],
        "hostname": d["hostname"], "ipv6_address": d["ipv6"],
        "source": "post",
    }]
    assert result["applied"] == 1
    assert result["results"][0]["deleted_rows"] == 1


def test_apply_actions_delete_zero_rowcount_is_recorded(monkeypatch):
    """A delete whose SQL matched nothing (e.g. a legacy row still untagged,
    so the source-scoped WHERE found no row) must still read applied:true
    (the apply itself succeeded) but record deleted_rows:0 -- the observable
    residue the checkpoint gate looks for, instead of a silent no-op
    masquerading as a clean apply."""
    from flax_classify import materializer

    monkeypatch.setattr(materializer, "delete_hosts_for_mac",
                        lambda pool, **kw: 0)
    actions = [("triage", "delete", MAC, {})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac={MAC: {"operator_note": False}})

    assert result["applied"] == 1
    assert result["results"][0]["applied"] is True
    assert result["results"][0]["deleted_rows"] == 0


def test_apply_actions_operator_note_delete_refused_and_counted(monkeypatch):
    from flax_classify import materializer

    def _fail(*a, **kw):
        raise AssertionError("must not delete an operator_note-flagged row")

    monkeypatch.setattr(materializer, "delete_hosts_for_mac", _fail)
    actions = [("triage", "delete", MAC, {})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac={MAC: {"operator_note": True}})

    assert result["applied"] == 0
    assert result["skipped_operator_note"] == 1
    assert result["apply_errors"] == 0
    assert result["results"][0]["skipped_operator_note"] is True
    assert result["results"][0]["applied"] is False


def test_apply_actions_operator_note_purge_handoff_skips_delete_but_still_upserts(
        monkeypatch):
    """operator_note protects the OLD owner's row from deletion, but a
    purge_handoff's upsert of the NEW owner's row is not itself a delete --
    it must still run (upsert_kea_host's conflict-merge preserves
    operator_note on the existing row)."""
    from flax_classify import materializer

    def _fail(*a, **kw):
        raise AssertionError("must not delete an operator_note-flagged row")

    monkeypatch.setattr(materializer, "delete_hosts_for_mac", _fail)
    upsert_calls = []
    monkeypatch.setattr(materializer, "upsert_kea_host",
                        lambda pool, **kw: upsert_calls.append(kw))
    d = _desired(owner_role="post")
    actions = [("post", "purge_handoff", MAC,
               {"from": "triage", "to": "post", "desired": _apply_detail(d)})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"post"}),
        max_deletes=20, actuals_by_mac={MAC: {"operator_note": True}})

    assert len(upsert_calls) == 1
    assert result["applied"] == 1
    assert result["skipped_operator_note"] == 1


def test_apply_actions_breaker_trips_on_excess_deletes(monkeypatch):
    """21 planned deletes for role=triage (max_deletes default 20) trips the
    breaker: nothing is applied for triage, but an unrelated role's action
    (well under its own threshold) is entirely unaffected."""
    from flax_classify import materializer

    delete_calls, upsert_calls = [], []
    monkeypatch.setattr(materializer, "delete_hosts_for_mac",
                        lambda pool, **kw: delete_calls.append(kw) or 1)
    monkeypatch.setattr(materializer, "upsert_kea_host",
                        lambda pool, **kw: upsert_calls.append(kw))

    triage_macs = [f"aa:aa:aa:aa:{i:02x}:00" for i in range(21)]
    actions = [("triage", "delete", m, {}) for m in triage_macs]
    post_desired = _desired(owner_role="post", mac=MAC2)
    actions.append(("post", "upsert", MAC2,
                    {"reason": "missing", "desired": _apply_detail(post_desired)}))
    actuals_by_mac = {m: {"operator_note": False} for m in triage_macs}

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage", "post"}),
        max_deletes=20, actuals_by_mac=actuals_by_mac)

    assert delete_calls == [], "breaker must block every triage delete"
    assert result["breaker_tripped"] == ["triage"]
    assert result["applied"] == 1, "the unaffected post upsert still applies"
    assert len(upsert_calls) == 1
    triage_results = result["results"][:21]
    assert all(r["breaker"] is True and r["applied"] is False
              for r in triage_results)
    assert result["results"][21]["applied"] is True


def test_apply_actions_breaker_not_tripped_at_exactly_max_deletes(monkeypatch):
    """max_deletes is an inclusive bound: exactly 20 planned deletes must NOT
    trip the breaker (only strictly MORE than max_deletes does)."""
    from flax_classify import materializer

    monkeypatch.setattr(materializer, "delete_hosts_for_mac",
                        lambda pool, **kw: 1)
    triage_macs = [f"aa:aa:aa:aa:{i:02x}:00" for i in range(20)]
    actions = [("triage", "delete", m, {}) for m in triage_macs]
    actuals_by_mac = {m: {"operator_note": False} for m in triage_macs}

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac=actuals_by_mac)

    assert result["breaker_tripped"] == []
    assert result["applied"] == 20


def test_apply_actions_apply_error_captured_without_raise(monkeypatch):
    from flax_classify import materializer

    def _boom(pool, **kw):
        raise RuntimeError("kea write failed")

    monkeypatch.setattr(materializer, "upsert_kea_host", _boom)
    d = _desired()
    actions = [("triage", "upsert", MAC, {"reason": "missing", "desired": _apply_detail(d)})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac={})

    assert result["applied"] == 0
    assert result["apply_errors"] == 1
    assert "kea write failed" in result["results"][0]["apply_error"]
    assert result["results"][0]["applied"] is False


def test_apply_actions_role_not_enforced_is_never_applied(monkeypatch):
    from flax_classify import materializer

    def _fail(*a, **kw):
        raise AssertionError("must not be called for a non-enforced role")

    monkeypatch.setattr(materializer, "upsert_kea_host", _fail)
    monkeypatch.setattr(materializer, "delete_hosts_for_mac", _fail)
    d = _desired(owner_role="post")
    actions = [("post", "upsert", MAC, {"reason": "missing", "desired": _apply_detail(d)}),
              ("post", "delete", MAC2, {})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac={})

    assert result["applied"] == 0
    assert result["apply_errors"] == 0
    assert result["results"] == [
        {"applied": False, "apply_error": None,
         "skipped_operator_note": False, "breaker": False},
        {"applied": False, "apply_error": None,
         "skipped_operator_note": False, "breaker": False},
    ]


# ── Post-3b fix: apply-time relocation cleanup (same-owner vid moves) ───────
#
# upsert_kea_host's conflict key is (mac, type, dhcp4_subnet_id): a vid-drift
# upsert INSERTs a new-subnet row and leaves the old-subnet row behind. The
# legacy purge_relocated_mac_hosts was exactly this cleanup; post-3b the apply
# layer must do it itself or the mac becomes two actuals next cycle -->
# multi_actual skip --> frozen forever.

def test_apply_actions_upsert_with_other_vid_actual_runs_relocation_cleanup(
        monkeypatch):
    """A vid-drift upsert whose actual sits in a DIFFERENT subnet triggers
    delete_other_subnet_rows AFTER the upsert, with the desired row's owner as
    source and its vid as keep_vid; the helper's rowcount is recorded in the
    action's deleted_rows detail."""
    from flax_classify import materializer

    order = []
    monkeypatch.setattr(materializer, "upsert_kea_host",
                        lambda pool, **kw: order.append(("upsert", kw)))
    cleanup_calls = []

    def _cleanup(pool, **kw):
        order.append(("cleanup", kw))
        cleanup_calls.append(kw)
        return 1

    monkeypatch.setattr(materializer, "delete_other_subnet_rows", _cleanup)
    d = _desired(vid=24)
    actions = [("triage", "upsert", MAC,
                {"fields": ["vid"], "desired": _apply_detail(d)})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20,
        actuals_by_mac={MAC: _actual(vid=17, operator_note=False)})

    assert cleanup_calls == [{"mac": MAC, "source": "triage", "keep_vid": 24}]
    # cleanup runs AFTER the upsert succeeded (never before: a failed upsert
    # must not delete the only remaining row for the mac)
    assert [step for step, _ in order] == ["upsert", "cleanup"]
    assert result["applied"] == 1
    assert result["results"][0]["deleted_rows"] == 1


def test_apply_actions_upsert_same_vid_actual_skips_relocation_cleanup(
        monkeypatch):
    """No other-vid actual (hostname-only drift) -> the helper is NOT called;
    the call is gated on actuals_by_mac evidence, never issued speculatively."""
    from flax_classify import materializer

    monkeypatch.setattr(materializer, "upsert_kea_host", lambda pool, **kw: None)

    def _must_not_run(pool, **kw):
        raise AssertionError("relocation cleanup must not run without "
                             "other-vid evidence")

    monkeypatch.setattr(materializer, "delete_other_subnet_rows", _must_not_run)
    d = _desired(vid=16)
    actions = [("triage", "upsert", MAC,
                {"fields": ["hostname"], "desired": _apply_detail(d)})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20,
        actuals_by_mac={MAC: _actual(vid=16, operator_note=False)})

    assert result["applied"] == 1
    assert "deleted_rows" not in result["results"][0]


def test_apply_actions_upsert_missing_actual_skips_relocation_cleanup(
        monkeypatch):
    """A reason='missing' upsert (no actual row at all) never calls the
    helper -- there is no old-subnet row to strand."""
    from flax_classify import materializer

    monkeypatch.setattr(materializer, "upsert_kea_host", lambda pool, **kw: None)

    def _must_not_run(pool, **kw):
        raise AssertionError("relocation cleanup must not run for a missing-"
                             "actual upsert")

    monkeypatch.setattr(materializer, "delete_other_subnet_rows", _must_not_run)
    d = _desired()
    actions = [("triage", "upsert", MAC,
                {"reason": "missing", "desired": _apply_detail(d)})]

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac={})

    assert result["applied"] == 1
    assert "deleted_rows" not in result["results"][0]


def test_apply_actions_relocation_deletes_count_toward_breaker(monkeypatch):
    """Relocation cleanups are real kea deletes: 21 vid-move upserts (each
    contributing one relocation delete) with max_deletes=20 trip the breaker
    for the role BEFORE anything is written -- neither the upserts nor the
    cleanups run."""
    from flax_classify import materializer

    def _must_not_run(*a, **kw):
        raise AssertionError("breaker must block every write for the role")

    monkeypatch.setattr(materializer, "upsert_kea_host", _must_not_run)
    monkeypatch.setattr(materializer, "delete_other_subnet_rows", _must_not_run)

    macs = [f"aa:aa:aa:aa:{i:02x}:00" for i in range(21)]
    actions = []
    actuals_by_mac = {}
    for m in macs:
        d = _desired(mac=m, vid=24)
        actions.append(("triage", "upsert", m,
                        {"fields": ["vid"], "desired": _apply_detail(d)}))
        actuals_by_mac[m] = _actual(mac=m, vid=17, operator_note=False)

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac=actuals_by_mac)

    assert result["breaker_tripped"] == ["triage"]
    assert result["applied"] == 0
    assert all(r["breaker"] is True for r in result["results"])


def test_apply_actions_relocation_at_exactly_max_deletes_not_tripped(
        monkeypatch):
    """Inclusive bound parity with the delete-action breaker test: exactly
    max_deletes relocation-contributing upserts do NOT trip the breaker."""
    from flax_classify import materializer

    monkeypatch.setattr(materializer, "upsert_kea_host", lambda pool, **kw: None)
    monkeypatch.setattr(materializer, "delete_other_subnet_rows",
                        lambda pool, **kw: 1)

    macs = [f"aa:aa:aa:aa:{i:02x}:00" for i in range(20)]
    actions = []
    actuals_by_mac = {}
    for m in macs:
        d = _desired(mac=m, vid=24)
        actions.append(("triage", "upsert", m,
                        {"fields": ["vid"], "desired": _apply_detail(d)}))
        actuals_by_mac[m] = _actual(mac=m, vid=17, operator_note=False)

    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac=actuals_by_mac)

    assert result["breaker_tripped"] == []
    assert result["applied"] == 20


# ── Task 4: run_cycle orchestration (mocked pool, no DB) ────────────────────

def _mock_pool_conn():
    from unittest.mock import MagicMock

    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, cursor


def test_run_cycle_default_shadow_never_calls_apply_actions(monkeypatch):
    """The deployed default (enforced_roles=frozenset()) must never call
    apply_actions -- this is what makes shadow mode a true no-op on
    kea.hosts (materializer.run_cycle docstring)."""
    from flax_classify import materializer

    monkeypatch.setattr(materializer, "read_desired", lambda pool: [_desired()])
    monkeypatch.setattr(materializer, "read_kea_actuals", lambda pool: [])

    def _fail(*a, **kw):
        raise AssertionError("apply_actions must not be called under shadow default")

    monkeypatch.setattr(materializer, "apply_actions", _fail)
    pool, cursor = _mock_pool_conn()

    result = materializer.run_cycle(pool, {"triage"})

    assert result["planned"] == 1
    assert result["applied"] == 0
    assert result["apply_errors"] == 0
    assert result["skipped_operator_note"] == 0
    assert result["breaker_tripped"] == []
    # every plan row written unapplied (nothing was ever asked to apply)
    sql, rows = cursor.executemany.call_args[0]
    assert rows[0]["detail"].obj["applied"] is False


def test_run_cycle_enforced_calls_apply_actions_and_annotates_plan_rows(monkeypatch):
    from flax_classify import materializer

    d = _desired()
    monkeypatch.setattr(materializer, "read_desired", lambda pool: [d])
    monkeypatch.setattr(materializer, "read_kea_actuals", lambda pool: [])

    apply_calls = []

    def _fake_apply(pool, actions, *, enforced_roles, max_deletes, actuals_by_mac):
        apply_calls.append((enforced_roles, max_deletes, actuals_by_mac))
        return {"applied": 1, "apply_errors": 0, "skipped_operator_note": 0,
               "breaker_tripped": [],
               "results": [{"applied": True, "apply_error": None,
                            "skipped_operator_note": False, "breaker": False,
                            "deleted_rows": 0}]}

    monkeypatch.setattr(materializer, "apply_actions", _fake_apply)
    pool, cursor = _mock_pool_conn()

    result = materializer.run_cycle(
        pool, {"triage"}, enforced_roles=frozenset({"triage"}), max_deletes=5)

    assert len(apply_calls) == 1
    enforced_roles_arg, max_deletes_arg, actuals_by_mac_arg = apply_calls[0]
    assert enforced_roles_arg == frozenset({"triage"})
    assert max_deletes_arg == 5
    assert actuals_by_mac_arg == {}

    assert result["applied"] == 1
    assert result["apply_errors"] == 0
    assert result["skipped_operator_note"] == 0
    assert result["breaker_tripped"] == []

    sql, rows = cursor.executemany.call_args[0]
    assert len(rows) == 1
    assert rows[0]["detail"].obj["applied"] is True
    # deleted_rows from the apply result is threaded into the plan row --
    # the 0 here is the audit residue for a no-op delete.
    assert rows[0]["detail"].obj["deleted_rows"] == 0


def test_run_cycle_breaker_writes_unapplied_plan_rows(monkeypatch):
    """End-to-end (real apply_actions, monkeypatched kea writers + mocked
    pool): 21 planned deletes for role=triage trips the breaker -- their
    plan rows are written applied=false/breaker=true and
    delete_hosts_for_mac is never called; an unrelated role's action is
    unaffected."""
    from flax_classify import materializer

    triage_macs = [f"aa:aa:aa:aa:{i:02x}:00" for i in range(21)]
    actuals = [_actual(mac=m, source="triage") for m in triage_macs]
    post_desired = _desired(owner_role="post", mac=MAC2)

    monkeypatch.setattr(materializer, "read_desired", lambda pool: [post_desired])
    monkeypatch.setattr(materializer, "read_kea_actuals", lambda pool: actuals)

    delete_calls, upsert_calls = [], []
    monkeypatch.setattr(materializer, "delete_hosts_for_mac",
                        lambda pool, **kw: delete_calls.append(kw) or 1)
    monkeypatch.setattr(materializer, "upsert_kea_host",
                        lambda pool, **kw: upsert_calls.append(kw))

    pool, cursor = _mock_pool_conn()
    result = materializer.run_cycle(
        pool, {"triage", "post"},
        enforced_roles=frozenset({"triage", "post"}), max_deletes=20)

    assert delete_calls == []
    assert len(upsert_calls) == 1
    assert result["breaker_tripped"] == ["triage"]

    sql, rows = cursor.executemany.call_args[0]
    triage_rows = [r for r in rows if r["owner_role"] == "triage"]
    assert len(triage_rows) == 21
    assert all(r["detail"].obj["applied"] is False
              and r["detail"].obj["breaker"] is True
              for r in triage_rows)
    post_rows = [r for r in rows if r["owner_role"] == "post"]
    assert len(post_rows) == 1
    assert post_rows[0]["detail"].obj["applied"] is True


def test_run_shadow_is_still_a_thin_alias_with_truncated_shape(monkeypatch):
    """run_shadow must return ONLY {planned, by_action, skipped} -- even
    though run_cycle (which it now delegates to) computes a richer dict --
    so shadow-mode callers/tests keep their phase-2 exact-equality contract."""
    from flax_classify import materializer

    monkeypatch.setattr(materializer, "read_desired", lambda pool: [])
    monkeypatch.setattr(materializer, "read_kea_actuals", lambda pool: [])
    pool, cursor = _mock_pool_conn()

    result = materializer.run_shadow(pool, {"triage"})
    assert set(result) == {"planned", "by_action", "skipped"}


# ── DB-backed: run_shadow apply + retention ─────────────────────────────────

psycopg = pytest.importorskip("psycopg")

TEST_MAC_A = "aa:bb:cc:00:22:01"
TEST_MAC_B = "aa:bb:cc:00:22:02"


def _dsn() -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    return " ".join([
        "host=" + os.environ.get("PGHOST", "127.0.0.1"),
        "port=" + os.environ.get("PGPORT", "5432"),
        "user=" + os.environ.get("PGUSER", "postgres"),
        "password=" + os.environ.get("PGPASSWORD", ""),
        "dbname=" + os.environ.get("PGDATABASE", "flax"),
    ])


def _cleanup(conn) -> None:
    conn.execute("DELETE FROM materializer_plan WHERE mac = ANY(%s)",
                 ([TEST_MAC_A, TEST_MAC_B],))
    conn.execute("DELETE FROM desired_reservations WHERE mac = ANY(%s)",
                 ([TEST_MAC_A, TEST_MAC_B],))
    conn.execute("DELETE FROM mac_ownership_events WHERE mac = ANY(%s)",
                 ([TEST_MAC_A, TEST_MAC_B],))


@pytest.fixture()
def shadow_conn():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:
        pytest.skip("no DB available: " + str(exc))
    try:
        _cleanup(c)
        yield c
    finally:
        try:
            _cleanup(c)
        except Exception:
            pass
        c.close()


def _build_pool(dsn: str):
    from flax_classify.db import build_pool
    return build_pool(dsn)


def test_run_shadow_writes_plan_rows_sharing_one_run_id(shadow_conn):
    """DB-backed, hermetic against a live/populated gouda DB: this test seeds
    exactly two desired rows and only asserts on THOSE macs' plan rows (row-
    level presence + shared run key) plus the always-written summary row --
    never on `run_shadow`'s global returned counters, which reflect the
    WHOLE live desired_reservations/kea.hosts state and would include real
    triage drift already queued at deploy time (an exact `== 2` assertion
    there would fail against real data and is not what this test is for).
    Cleanup is by run key, not just by our two test macs, since the summary
    marker row this run also writes is keyed mac='-' and would otherwise
    never be swept by the shadow_conn fixture's mac-scoped cleanup.
    """
    from flax_classify.desired_reservations import upsert_desired
    from flax_classify.materializer import run_shadow

    pool = _build_pool(_dsn())
    run_key = None
    try:
        upsert_desired(pool, owner_role="triage", mac=TEST_MAC_A, kind="bmc",
                       hostname="a-bmc", ipv4="10.0.0.1", ipv6=None,
                       vid=16, switch="rabbit-test-mz", port="et6b1")
        upsert_desired(pool, owner_role="triage", mac=TEST_MAC_B, kind="bmc",
                       hostname="b-bmc", ipv4="10.0.0.2", ipv6=None,
                       vid=16, switch="rabbit-test-mz", port="et6b2")

        summary = run_shadow(pool, {"triage"})
        # Tolerate unrelated live drift: our two macs must be AMONG whatever
        # was planned, not the only things planned.
        assert summary["planned"] >= 2
        assert summary["by_action"].get("upsert", 0) >= 2

        rows = shadow_conn.execute(
            "SELECT mac, (detail->>'run') FROM materializer_plan "
            "WHERE mac = ANY(%s) ORDER BY mac",
            ([TEST_MAC_A, TEST_MAC_B],),
        ).fetchall()
        assert [r[0] for r in rows] == [TEST_MAC_A, TEST_MAC_B]
        assert rows[0][1] == rows[1][1], "one cycle's rows share the same run id"
        run_key = rows[0][1]
        assert run_key is not None
        from flax_classify import materializer
        assert run_key.startswith(materializer._RUN_TOKEN + ":"), \
            "run key is '<process-token>:<counter>'"

        summary_row = shadow_conn.execute(
            "SELECT owner_role, mac, detail FROM materializer_plan "
            "WHERE action = 'summary' AND (detail->>'run') = %s",
            (run_key,),
        ).fetchone()
        assert summary_row is not None, \
            "run_shadow must write exactly one summary row per run"
        owner_role, mac, detail = summary_row
        assert (owner_role, mac) == ("-", "-")
        assert detail["run"] == run_key
        assert detail["planned"] == summary["planned"]
        assert detail["by_action"] == summary["by_action"]
        assert detail["skipped"] == summary["skipped"]
    finally:
        if run_key is not None:
            shadow_conn.execute(
                "DELETE FROM materializer_plan WHERE (detail->>'run') = %s",
                (run_key,))
        pool.close()


def test_run_shadow_retention_deletes_old_rows(shadow_conn):
    from flax_classify.materializer import run_shadow

    pool = _build_pool(_dsn())
    try:
        shadow_conn.execute(
            "INSERT INTO materializer_plan (ts, owner_role, action, mac, detail) "
            "VALUES (now() - interval '8 days', 'triage', 'upsert', %s, '{}')",
            (TEST_MAC_A,),
        )
        run_shadow(pool, {"triage"})
        row = shadow_conn.execute(
            "SELECT 1 FROM materializer_plan WHERE mac = %s", (TEST_MAC_A,),
        ).fetchone()
        assert row is None, "8-day-old plan row must be swept by 7-day retention"
    finally:
        pool.close()


def test_apply_actions_runs_deletes_before_upserts_but_keeps_result_order(monkeypatch):
    """Part 2 R5: on a confirmed BMC MAC flip the new mac's upsert wants the
    SAME ipv4 as the old mac's row. Actions arrive sorted by mac, so the
    upsert can come first and collide; deletes must run first. results stay
    index-aligned with actions (run_cycle zips them into plan rows)."""
    from flax_classify import materializer

    order = []
    monkeypatch.setattr(materializer, "_apply_upsert",
                        lambda pool, role, mac, detail: order.append(("upsert", mac)))
    monkeypatch.setattr(materializer, "delete_hosts_for_mac",
                        lambda pool, **kw: order.append(("delete", kw["mac"])) or 1)
    monkeypatch.setattr(materializer, "_needs_relocation_cleanup",
                        lambda actual, detail: False)
    actions = [
        ("triage", "upsert", "aa:00:00:00:00:01", {"desired": {"vid": 17}}),
        ("triage", "delete", "aa:00:00:00:00:02", {}),
        ("triage", "upsert", "aa:00:00:00:00:03", {"desired": {"vid": 17}}),
    ]
    result = materializer.apply_actions(
        object(), actions, enforced_roles=frozenset({"triage"}),
        max_deletes=20, actuals_by_mac={})
    assert order == [("delete", "aa:00:00:00:00:02"),
                     ("upsert", "aa:00:00:00:00:01"),
                     ("upsert", "aa:00:00:00:00:03")]
    assert [r.get("deleted_rows") for r in result["results"]] == [None, 1, None]
    assert result["applied"] == 3
