"""Pure view functions for the read-only /shadow page.

DB-read only: this module never touches the filesystem and imports nothing
from flax_classify -- it only shapes rows already fetched from the
`materializer_plan` / `mac_ownership_events` / `desired_reservations` tables
(queries.py) into template-ready structures. Mirrors roles_view.py's split
between pure builders here and SQL in queries.py.

Latest-run grouping (read this before touching `latest_rows` below):
  `detail->>'run'` is the key minted by `flax_classify.materializer._next_run_id`
  -- a `"<process-token>:<counter>"` string, unique across restarts because the
  token is random per process lifetime (a bare counter would collide with the
  prior lifetime's counter at the same integer). CONTIGUITY is kept as a
  defense-in-depth disambiguator on top of that: run_shadow writes all of one
  run's rows (its planned actions plus the summary marker row, see below) in
  one transaction, so a run is exactly one contiguous id block in the table.
  "The latest run" is thus the leading contiguous prefix of the id-DESC row
  stream sharing the newest row's run value -- never a filter of the whole
  fetched window by that value, which would union stale same-value rows from
  an unrelated run into the convergence snapshot.

Convergence semantics (read this before touching `converged` below):
  Every `run_shadow` call now writes exactly one marker row per run --
  `(owner_role="-", action="summary", mac="-", detail={"run", "planned",
  "by_action", "skipped"})` -- REGARDLESS of whether the planner produced any
  actions. This makes the latest run always discoverable from the plan table
  alone: an empty `materializer_plan` table means the materializer has simply
  never run (no more "converged forever" vs. "hasn't run recently"
  ambiguity). Convergence is read directly off the latest run's summary row
  (`planned == 0`), not inferred by counting non-summary rows.

  `skipped` (the summary detail's skip counters -- unowned /
  unregistered_desired / multi_actual macs the planner deliberately did not
  plan against, see `flax_classify.materializer.plan_materialization`) is
  surfaced separately so a converged-but-skipping run does not read as a
  clean green convergence: the page shows it as "converged, N macs skipped
  from planning" and the caller renders it amber, not green.
"""
import collections
from typing import Any


def is_stale(latest_plan_ts, now, stale_secs):
    """The write-freeze detector (post-3b ops note, given a face): the
    materializer is the SOLE kea.hosts writer, so a missing or aging latest
    plan-summary timestamp means reservation writes have gone silent.
    latest_plan_ts None (never ran / registry-degraded start) is stale by
    definition."""
    if latest_plan_ts is None:
        return True
    return (now - latest_plan_ts).total_seconds() > stale_secs


def _run_of(detail: Any) -> Any:
    """Extract detail->>'run' from a materializer_plan detail jsonb value.
    detail arrives already deserialized to a dict (or None/empty)."""
    return (detail or {}).get("run")


def _empty_result(shaped_plan_rows: list, shaped_events: list,
                  shaped_desired: list) -> dict:
    return {
        "ran": False,
        "converged": None,
        "latest_run": None,
        "latest_plan_ts": None,
        "planned_count": 0,
        "by_owner_action": [],
        "skipped": {},
        "skip_total": 0,
        "plan_rows": shaped_plan_rows,
        "events": shaped_events,
        "desired_summary": shaped_desired,
    }


def build_shadow(plan_rows: list, event_rows: list, desired_rows: list) -> dict:
    """Shape (materializer_plan, mac_ownership_events, desired_reservations
    summary) rows into the /shadow page model.

    plan_rows: [(ts, owner_role, action, mac, detail:dict), ...] ordered
        newest-first (DESC by id) -- see queries.materializer_recent(). One
        row per run is the "summary" marker (owner_role="-", mac="-") written
        by every `run_shadow` call regardless of outcome -- see module
        docstring.
    event_rows: [(at, mac, from_role, to_role, switch, port), ...] ordered
        newest-first -- see queries.ownership_events_recent().
    desired_rows: [(owner_role, count, max_updated_at), ...] -- see
        queries.desired_summary().

    Returns:
      {"ran": bool,                # False only when plan_rows is empty --
                                    #   the materializer has never run
       "converged": bool|None,     # latest run's summary planned == 0;
                                    #   None when ran is False
       "latest_run": str|None,
       "latest_plan_ts": ts|None,
       "planned_count": int,        # from the latest run's summary detail
                                    #   (falls back to counting non-summary
                                    #   rows if a summary row is somehow
                                    #   missing)
       "by_owner_action": [{"owner_role","action","count"}, ...],  # latest
           run only, summary row excluded, sorted by (owner_role, action)
       "skipped": {"unowned": int, "unregistered_desired": int,
           "multi_actual": int},  # from the latest run's summary detail
       "skip_total": int,          # sum of the skipped counters
       "plan_rows": [{"ts","owner_role","action","mac","detail"}, ...],  # ALL
           fetched rows (full recent history, not just the latest run)
       "events": [{"at","mac","from_role","to_role","switch","port"}, ...],
       "desired_summary": [{"owner_role","count","max_updated_at"}, ...]}

    Empty plan_rows -> ran=False, converged=None, latest_run=None (see module
    docstring: this now unambiguously means "never run"). Empty event_rows /
    desired_rows shape to empty lists -- no crash, template renders its
    per-table empty state.
    """
    shaped_plan_rows = [
        {"ts": ts, "owner_role": owner_role, "action": action, "mac": mac,
         "detail": detail or {}}
        for ts, owner_role, action, mac, detail in plan_rows
    ]
    shaped_events = [
        {"at": at, "mac": mac, "from_role": from_role, "to_role": to_role,
         "switch": switch, "port": port}
        for at, mac, from_role, to_role, switch, port in event_rows
    ]
    shaped_desired = [
        {"owner_role": owner_role, "count": count, "max_updated_at": max_updated_at}
        for owner_role, count, max_updated_at in desired_rows
    ]

    if not plan_rows:
        return _empty_result(shaped_plan_rows, shaped_events, shaped_desired)

    latest_ts = plan_rows[0][0]
    latest_run = _run_of(plan_rows[0][4])
    # Latest run = the LEADING CONTIGUOUS PREFIX of the newest-first rows,
    # NOT every row in the window whose run value equals latest_run -- see
    # module docstring.
    latest_rows = []
    for r in plan_rows:
        if _run_of(r[4]) != latest_run:
            break
        latest_rows.append(r)

    summary_row = next((r for r in latest_rows if r[2] == "summary"), None)
    action_rows = [r for r in latest_rows if r[2] != "summary"]

    if summary_row is not None:
        summary_detail = summary_row[4] or {}
        planned_count = summary_detail.get("planned", len(action_rows))
        skipped = dict(summary_detail.get("skipped") or {})
    else:
        # Defensive fallback only -- run_shadow always writes a summary row
        # for every run. Degrade to counting instead of crashing.
        planned_count = len(action_rows)
        skipped = {}

    skip_total = sum(skipped.values())

    counts: collections.Counter = collections.Counter(
        (r[1], r[2]) for r in action_rows
    )
    by_owner_action = [
        {"owner_role": owner_role, "action": action, "count": n}
        for (owner_role, action), n in sorted(counts.items())
    ]

    return {
        "ran": True,
        "converged": planned_count == 0,
        "latest_run": latest_run,
        "latest_plan_ts": latest_ts,
        "planned_count": planned_count,
        "by_owner_action": by_owner_action,
        "skipped": skipped,
        "skip_total": skip_total,
        "plan_rows": shaped_plan_rows,
        "events": shaped_events,
        "desired_summary": shaped_desired,
    }
