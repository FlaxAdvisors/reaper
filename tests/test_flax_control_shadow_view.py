"""Tests for flax_control.shadow_view — pure view functions for /shadow.

No DB, no FastAPI client: build_shadow() is fed literal row fixtures shaped
exactly like the materializer_plan / mac_ownership_events / desired_reservations
columns (mirrors test_flax_control_roles_view.py's approach for build_registry).

Every `run_shadow` call now writes exactly one "summary" marker row per run
(owner_role="-", action="summary", mac="-", detail carries "planned"/
"by_action"/"skipped") in ADDITION to any planned action rows, and — because
it is inserted after the action-row executemany in the same transaction — it
has the highest id in its run, so it is the FIRST (newest) row of that run's
block in the id-DESC fetch order. Fixtures below reflect that real shape.
"""
import datetime

from flax_control import shadow_view

T1 = datetime.datetime(2026, 7, 4, 10, 0, 0)
T2 = datetime.datetime(2026, 7, 4, 10, 5, 0)
T3 = datetime.datetime(2026, 7, 4, 10, 10, 0)


def _summary_row(ts, run, planned, by_action=None, skipped=None):
    return (ts, "-", "summary", "-",
            {"run": run, "planned": planned, "by_action": by_action or {},
             "skipped": skipped or {"unowned": 0, "unregistered_desired": 0,
                                    "multi_actual": 0}})


# ---------------------------------------------------------------------------
# never ran: empty plan table
# ---------------------------------------------------------------------------

def test_never_ran_when_plan_rows_empty():
    out = shadow_view.build_shadow([], [], [])
    assert out["ran"] is False
    assert out["converged"] is None
    assert out["latest_run"] is None
    assert out["latest_plan_ts"] is None
    assert out["planned_count"] == 0
    assert out["by_owner_action"] == []
    assert out["skipped"] == {}
    assert out["skip_total"] == 0
    assert out["plan_rows"] == []
    assert out["events"] == []
    assert out["desired_summary"] == []


def test_never_ran_plan_rows_empty_but_events_and_desired_present():
    """An empty plan table doesn't force the OTHER tables empty too — the
    ownership ledger / desired-reservations summary are written by a
    different path (flax_classify/desired_reservations.py) than the
    materializer, so they can be populated even if run_shadow has never
    executed."""
    event_rows = [(T1, "aa:bb:cc:00:00:01", None, "triage", "rabbit-gouda", "et6/1")]
    desired_rows = [("triage", 5, T1)]
    out = shadow_view.build_shadow([], event_rows, desired_rows)
    assert out["ran"] is False
    assert out["converged"] is None
    assert out["events"] == [
        {"at": T1, "mac": "aa:bb:cc:00:00:01", "from_role": None,
         "to_role": "triage", "switch": "rabbit-gouda", "port": "et6/1"}
    ]
    assert out["desired_summary"] == [
        {"owner_role": "triage", "count": 5, "max_updated_at": T1}
    ]


# ---------------------------------------------------------------------------
# converged, clean: latest run's summary shows planned == 0, no skips
# ---------------------------------------------------------------------------

def test_converged_clean_no_skips():
    plan_rows = [_summary_row(T1, "abc:3", 0)]
    out = shadow_view.build_shadow(plan_rows, [], [])
    assert out["ran"] is True
    assert out["converged"] is True
    assert out["planned_count"] == 0
    assert out["skipped"] == {"unowned": 0, "unregistered_desired": 0,
                              "multi_actual": 0}
    assert out["skip_total"] == 0
    assert out["by_owner_action"] == []
    assert out["latest_run"] == "abc:3"
    assert out["latest_plan_ts"] == T1


# ---------------------------------------------------------------------------
# converged, WITH skips: must be flagged (amber, not green) via skip_total
# ---------------------------------------------------------------------------

def test_converged_with_skips_surfaces_skip_counters():
    plan_rows = [_summary_row(
        T1, "abc:4", 0,
        skipped={"unowned": 2, "unregistered_desired": 1, "multi_actual": 0})]
    out = shadow_view.build_shadow(plan_rows, [], [])
    assert out["ran"] is True
    assert out["converged"] is True, "planned == 0 is still convergence"
    assert out["skipped"] == {"unowned": 2, "unregistered_desired": 1,
                              "multi_actual": 0}
    assert out["skip_total"] == 3, "amber-vs-green in the template keys off this"
    assert out["planned_count"] == 0
    assert out["by_owner_action"] == []


# ---------------------------------------------------------------------------
# non-converged: latest run has actions, grouped per owner/action
# ---------------------------------------------------------------------------

# plan_rows ordered newest-first (DESC by id), as materializer_recent() returns.
# Latest run = "2" (rows at index 0-3, summary row first/newest); an older run
# "1" (rows at index 4-6, its own summary row first/newest within its block)
# is still present in the table (not yet swept by retention) but must NOT be
# counted in planned_count / by_owner_action.
PLAN_ROWS_MIXED = [
    _summary_row(T3, 2, 3, by_action={"upsert": 2, "delete": 1}),
    (T3, "post", "upsert", "aa:bb:cc:00:00:03", {"run": 2, "reason": "missing"}),
    (T3, "triage", "delete", "aa:bb:cc:00:00:02", {"run": 2}),
    (T3, "triage", "upsert", "aa:bb:cc:00:00:01", {"run": 2, "fields": ["hostname"]}),
    _summary_row(T2, 1, 2, by_action={"upsert": 1, "delete": 1}),
    (T2, "triage", "upsert", "aa:bb:cc:00:00:09", {"run": 1, "reason": "missing"}),
    (T1, "post", "delete", "aa:bb:cc:00:00:08", {"run": 1}),
]


def test_non_converged_latest_run_only_counted():
    out = shadow_view.build_shadow(PLAN_ROWS_MIXED, [], [])
    assert out["ran"] is True
    assert out["converged"] is False
    assert out["latest_run"] == 2
    assert out["latest_plan_ts"] == T3
    assert out["planned_count"] == 3


def test_non_converged_by_owner_action_grouping():
    out = shadow_view.build_shadow(PLAN_ROWS_MIXED, [], [])
    assert out["by_owner_action"] == [
        {"owner_role": "post", "action": "upsert", "count": 1},
        {"owner_role": "triage", "action": "delete", "count": 1},
        {"owner_role": "triage", "action": "upsert", "count": 1},
    ]


def test_non_converged_plan_rows_shape_includes_full_history_not_just_latest_run():
    """The plan table on the page shows the full recent-history fetch (both
    run 1 and run 2 rows, summary markers included), while
    by_owner_action/planned_count are scoped to the latest run's summary only
    -- these are two different views over the same rows. The newest row is
    the latest run's OWN summary marker (highest id in its run's block)."""
    out = shadow_view.build_shadow(PLAN_ROWS_MIXED, [], [])
    assert len(out["plan_rows"]) == 7
    assert out["plan_rows"][0] == {
        "ts": T3, "owner_role": "-", "action": "summary", "mac": "-",
        "detail": {"run": 2, "planned": 3, "by_action": {"upsert": 2, "delete": 1},
                  "skipped": {"unowned": 0, "unregistered_desired": 0,
                              "multi_actual": 0}},
    }
    # the real action row is present in the full list...
    assert any(r["mac"] == "aa:bb:cc:00:00:03" and r["action"] == "upsert"
               for r in out["plan_rows"])
    # older run's row is present in the full list...
    assert any(r["mac"] == "aa:bb:cc:00:00:08" for r in out["plan_rows"])
    # ...but not folded into the latest-run grouping.
    assert not any(
        r["action"] == "delete" and r["owner_role"] == "post"
        for r in out["by_owner_action"]
    )


def test_non_converged_single_run_all_rows_counted():
    plan_rows = [
        _summary_row(T1, 7, 2, by_action={"upsert": 2}),
        (T1, "triage", "upsert", "aa:bb:cc:00:00:01", {"run": 7}),
        (T1, "triage", "upsert", "aa:bb:cc:00:00:02", {"run": 7}),
    ]
    out = shadow_view.build_shadow(plan_rows, [], [])
    assert out["converged"] is False
    assert out["latest_run"] == 7
    assert out["planned_count"] == 2
    assert out["by_owner_action"] == [
        {"owner_role": "triage", "action": "upsert", "count": 2},
    ]


def test_latest_run_grouping_stops_at_first_run_boundary_not_whole_window():
    """The materializer's `run` id is a PER-PROCESS-lifetime key that can
    still recur in a hand-rolled fixture's small integer space. The latest
    run must be the LEADING CONTIGUOUS PREFIX of the newest-first rows (one
    run_shadow call writes one temporally contiguous id block, summary row
    included), NOT every row in the window sharing the same run value — else
    stale rows from an unrelated run (days old, same run=1 value) corrupt
    planned_count/by_owner_action."""
    plan_rows = [
        # newest run: run=1, its own summary marker leads the block
        _summary_row(T3, 1, 2, by_action={"upsert": 2}),
        (T3, "triage", "upsert", "aa:bb:cc:00:00:01", {"run": 1, "fields": ["ipv4"]}),
        (T3, "triage", "upsert", "aa:bb:cc:00:00:02", {"run": 1, "reason": "missing"}),
        # a distinct, older run (run=2 in this fixture's numbering)
        (T2, "post", "delete", "aa:bb:cc:00:00:07", {"run": 2}),
        # ...and an even older run that happens to reuse the value run=1:
        # same value as the newest run, but NOT the same run.
        (T1, "post", "upsert", "aa:bb:cc:00:00:08", {"run": 1, "reason": "missing"}),
        (T1, "post", "delete", "aa:bb:cc:00:00:09", {"run": 1}),
    ]
    out = shadow_view.build_shadow(plan_rows, [], [])
    assert out["latest_run"] == 1
    assert out["planned_count"] == 2  # only the leading contiguous block
    assert out["by_owner_action"] == [
        {"owner_role": "triage", "action": "upsert", "count": 2},
    ]
    # the stale run=1-valued block from the unrelated older run must not leak in
    assert not any(r["owner_role"] == "post" for r in out["by_owner_action"])
    # full history still shows everything
    assert len(out["plan_rows"]) == 6


# ---------------------------------------------------------------------------
# empty tables across the board -> empty-state dict, no crash
# ---------------------------------------------------------------------------

def test_all_empty_no_crash():
    out = shadow_view.build_shadow([], [], [])
    assert out == {
        "ran": False,
        "converged": None,
        "latest_run": None,
        "latest_plan_ts": None,
        "planned_count": 0,
        "by_owner_action": [],
        "skipped": {},
        "skip_total": 0,
        "plan_rows": [],
        "events": [],
        "desired_summary": [],
    }


def test_missing_summary_row_falls_back_to_counting_action_rows():
    """Defensive: if the latest run's block somehow has no 'summary' row
    (shouldn't happen given run_shadow always writes one), planned_count
    falls back to counting non-summary rows rather than raising."""
    plan_rows = [(T1, "triage", "upsert", "aa:bb:cc:00:00:01", {"reason": "missing"})]
    out = shadow_view.build_shadow(plan_rows, [], [])
    assert out["ran"] is True
    assert out["latest_run"] is None
    assert out["planned_count"] == 1
    assert out["skipped"] == {}
    assert out["by_owner_action"] == [
        {"owner_role": "triage", "action": "upsert", "count": 1},
    ]


def test_null_detail_treated_as_empty_dict():
    """detail is NOT NULL DEFAULT '{}' at the schema level, but guard against
    a raw None coming through a hand-rolled row anyway."""
    plan_rows = [(T1, "triage", "upsert", "aa:bb:cc:00:00:01", None)]
    out = shadow_view.build_shadow(plan_rows, [], [])
    assert out["plan_rows"][0]["detail"] == {}
    assert out["latest_run"] is None
