"""Pure view functions for the three /layers/* health pages.

Dashboards-as-indices (phase-5 spec): every panel links to the page that
explains it; these pages introduce no new data, only a layer-shaped read of
tables flax_control already consumes. Builders are pure — routes fetch and
pass rows + now/thresholds in.
"""
from . import shadow_view

_STATE_BADGE = {"healthy": "ok", "stale": "warn", "failed": "err",
                "missing": "err"}

# Which consumer_acks names each layer page is "about" — used both for the
# per-service ack panels (already existed) and now to scope the config-map
# filter, the anomalies query and the events feed to just this layer's
# services (routes fetch; this is the shared vocabulary the routes key off).
_LAYER_SERVICES = {
    "sensing": ("flax-switch-sense", "flax-observe", "flax-discover"),
    "policy": ("flax-classify",),
    "actuation": ("flax-reconcile",),
}


def panel(label, value, *, badge=None, href=None, hint=None):
    return {"label": label, "value": value, "badge": badge, "href": href,
            "hint": hint}


def _ack_panels(health_rows, consumers):
    out = []
    by_name = {h["consumer"]: h for h in (health_rows or [])}
    for name in consumers:
        h = by_name.get(name) or {}
        state = h.get("state", "missing")
        detail = h.get("detail") if isinstance(h.get("detail"), dict) else {}
        reason = detail.get("reason")
        val = state + (f" → {reason}" if reason else "")
        out.append(panel(f"{name} ack", val, badge=_STATE_BADGE.get(state, "warn"),
                         href="/services"))
    return out


def build_sensing(health_rows, switches_rows, observe_stats, lease_count,
                  config_rows=None, anomalies=None, events=None):
    """'Are my eyes open?' — sensing-service acks, switch polling, observe
    coverage, lease activity."""
    panels = _ack_panels(health_rows,
                         ("flax-switch-sense", "flax-observe", "flax-discover"))
    rows = switches_rows or []
    reachable = sum(1 for s in rows if s.get("reachable"))
    panels.append(panel("switches reachable",
                        f"{reachable}/{len(rows)}",
                        badge="ok" if rows and reachable == len(rows) else "warn",
                        href="/switches"))
    count, age = observe_stats or (0, None)
    panels.append(panel("observe_state rows", count,
                        hint=(f"oldest poll {int(age)}s ago" if age is not None else None),
                        badge="warn" if (age or 0) > 300 else None,
                        href="/devices"))
    panels.append(panel("active leases", lease_count, href="/leases"))
    return {"panels": panels, "config": config_rows or [],
           "anomalies": anomalies or [], "events": events or []}


def build_policy(health_rows, registry, desired_rows, uncovered_count,
                 config_rows=None, anomalies=None, events=None):
    """'Is the brain deciding, from fresh facts?'"""
    panels = _ack_panels(health_rows, ("flax-classify",))
    roles = (registry or {}).get("roles", [])
    gen = max((r["generation"] for r in roles), default=0)
    panels.append(panel("registry", f"{len(roles)} roles · gen {gen}",
                        badge="ok" if roles else "err", href="/roles",
                        hint=None if roles else "no roles published"))
    for owner_role, count, max_updated_at in (desired_rows or []):
        panels.append(panel(f"desired ({owner_role})", count,
                            hint=f"fresh {max_updated_at}", href="/shadow"))
    panels.append(panel("unassigned access ports", uncovered_count,
                        badge="warn" if uncovered_count else "ok",
                        href="/roles"))
    return {"panels": panels, "config": config_rows or [],
           "anomalies": anomalies or [], "events": events or []}


def build_actuation(health_rows, shadow_model, kea_count, cadence_rows,
                    flap_count, now, stale_secs,
                    config_rows=None, anomalies=None, events=None):
    """'Are hands moving, and only as instructed?'"""
    panels = _ack_panels(health_rows, ("flax-reconcile",))
    m = shadow_model or {}
    stale = shadow_view.is_stale(m.get("latest_plan_ts"), now, stale_secs)
    if stale:
        panels.append(panel("materializer", "NO FRESH CYCLE", badge="err",
                            href="/shadow", hint="write-freeze detector"))
    elif m.get("converged"):
        panels.append(panel("materializer", "converged", badge="ok",
                            href="/shadow"))
    else:
        panels.append(panel("materializer",
                            f"{m.get('planned_count', 0)} planned",
                            badge="warn", href="/shadow"))
    panels.append(panel("kea.hosts", kea_count, href="/reservations"))
    total = sum(n for _, _, n in (cadence_rows or []))
    panels.append(panel("reconcile actions (1h)", total,
                        hint=", ".join(f"{a}/{r}: {n}" for a, r, n in
                                       (cadence_rows or [])) or None,
                        href="/events"))
    panels.append(panel("intentional flaps active", flap_count,
                        badge="warn" if flap_count else None, href="/events"))
    return {"panels": panels, "config": config_rows or [],
           "anomalies": anomalies or [], "events": events or []}
