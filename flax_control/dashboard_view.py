"""Pure shaping of the status dashboard hero: the pipeline strip (axis 1) and
role lanes (axis 2). Consumes app.consumer_health() output + queries tuples."""

_PIPELINE = [
    ("discover", "Discover", "flax-discover"),
    ("switch-sense", "Switch-sense", "flax-switch-sense"),
    ("observe", "Observe", "flax-observe"),
    ("classify", "Classify", "flax-classify"),
    ("reconcile", "Reconcile", "flax-reconcile"),
]


def build_pipeline(health):
    by = {h["consumer"]: h for h in (health or [])}
    stages, worst = [], "healthy"
    # degraded (a partial per-source outage) ranks above stale/missing but below
    # a full failure. Keep this in sync with consumer_health()'s states.
    order = {"healthy": 0, "stale": 1, "missing": 1, "degraded": 2, "failed": 3}
    for key, label, service in _PIPELINE:
        h = by.get(service) or {"state": "missing"}
        state = h.get("state", "missing")
        if order.get(state, 1) > order.get(worst, 0):
            worst = state
        stages.append({"key": key, "label": label, "service": service,
                       "state": state, "action": h.get("detail"),
                       "ack_age": h.get("consumed_at")})
    stages.append({"key": "control", "label": "Control", "service": "flax-control",
                   "state": "healthy", "action": "reads", "ack_age": None})
    overall = {"healthy": "operational", "stale": "degraded",
               "missing": "degraded", "degraded": "degraded",
               "failed": "critical"}[worst]
    return {"stages": stages, "overall": overall}


def build_role_lanes(desired_by_role_kind):
    """Reduce [(owner_role, kind, count, ...)...] to per-role totals.

    Only the first three positions (owner_role, kind, count) are used, so this
    tolerates both the test's bare 3-tuples and queries.desired_by_role_kind()'s
    real 4-tuples (owner_role, kind, count, max_updated_at).
    """
    totals: dict = {}
    for row in (desired_by_role_kind or []):
        owner_role, count = row[0], row[2]
        totals[owner_role] = totals.get(owner_role, 0) + (count or 0)
    return [{"role": r, "dut_count": totals[r]} for r in sorted(totals)]
