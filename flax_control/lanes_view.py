"""Pure view functions for the /lanes per-role status page.

CORE TABLES ONLY (operator decision, phase-5 spec): everything here shapes
registry / desired_reservations / materializer_plan / work_records /
mac_ownership_events rows — flax_control holds no grant on any post-owned
table, and lane cards LINK to role UIs rather than reimplementing them.
Registry-driven: a new role (integra) gets a card with zero code change.
"""
from . import shadow_view


def parse_role_ui_links(raw):
    """'post=http://x,triage=http://y' -> {'post': 'http://x', ...}.
    Empty/malformed segments are dropped (a bad env var must not 500 the page)."""
    out = {}
    for seg in (raw or "").split(","):
        if "=" in seg:
            role, _, url = seg.partition("=")
            if role.strip() and url.strip():
                out[role.strip()] = url.strip()
    return out


def build_lanes(registry, desired_rows, shadow_model, record_rows, roam_rows,
                role_ui_links, now, stale_secs):
    """One card per registered role.

    registry: roles_view.build_registry() output. desired_rows:
    queries.desired_by_role_kind(). shadow_model: shadow_view.build_shadow()
    output (latest-run logic reused, not re-implemented). record_rows:
    queries.work_record_counts(). roam_rows: queries.roaming_role_counts_24h().
    """
    desired_by_role: dict = {}
    for owner_role, kind, count, max_updated_at in (desired_rows or []):
        desired_by_role.setdefault(owner_role, []).append(
            {"kind": kind, "count": count, "max_updated_at": max_updated_at})
    records_by_role: dict = {}
    for owner_role, kind, n_24h, n_7d in (record_rows or []):
        records_by_role.setdefault(owner_role, []).append(
            {"kind": kind, "n_24h": n_24h, "n_7d": n_7d})
    roam_by_role = {role: (inbound, outbound)
                    for role, inbound, outbound in (roam_rows or [])}
    planned_by_role: dict = {}
    for entry in (shadow_model or {}).get("by_owner_action", []):
        planned_by_role[entry["owner_role"]] = \
            planned_by_role.get(entry["owner_role"], 0) + entry["count"]
    stale = shadow_view.is_stale(
        (shadow_model or {}).get("latest_plan_ts"), now, stale_secs)

    cards = []
    for role in (registry or {}).get("roles", []):
        name = role["name"]
        roam_in, roam_out = roam_by_role.get(name, (0, 0))
        cards.append({
            "name": name,
            "generation": role["generation"],
            "universe_summary": role["universe_summary"],
            "record_keys": role["record_keys"],
            "desired": desired_by_role.get(name, []),
            "planned_actions": planned_by_role.get(name, 0),
            "converged": (shadow_model or {}).get("converged"),
            "materializer_stale": stale,
            "records": records_by_role.get(name, []),
            "roam_in": roam_in,
            "roam_out": roam_out,
            "ui_link": (role_ui_links or {}).get(name),
        })
    return cards
