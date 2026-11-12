"""Registry-driven observe scope (mirrors flax_reconcile/role_caps.py).

flax-observe enrolls the access ports of every switch claimed by a role whose
definition["capabilities"]["observe"] is truthy. Opt-in: a role that never
declares the capability is not observed. This replaces the hand-maintained
/etc/flax/rabbit-geometry.json with the published role registry as the single
source of truth. flax_observe already holds SELECT on roles/role_universe
(migration 029 _READERS), so no new grant.
"""
import logging

from psycopg_pool import ConnectionPool

log = logging.getLogger("flax-observe.role_caps")


def _observe_switches_from_rows(role_rows, universe_rows) -> frozenset:
    """Pure: role_rows=[(role, definition|None)], universe_rows=[(role, kind,
    switch)] -> frozenset of switch names for observe-capable roles."""
    eligible = {role for role, definition in role_rows
                if ((definition or {}).get("capabilities") or {}).get("observe")}
    return frozenset(
        sw for role, kind, sw in universe_rows
        if kind == "switch" and sw and role in eligible)


def read_observe_eligible_switches(pool: ConnectionPool):
    """SELECT from roles + role_universe -> frozenset[str] | None.

    None means "registry empty/unreadable" -> caller keeps pure static
    behaviour (no dynamic enrollment), the safe deploy-order fallback.
    """
    try:
        with pool.connection() as conn:
            role_rows = conn.execute(
                "SELECT role, definition FROM roles").fetchall()
            universe_rows = conn.execute(
                "SELECT role, kind, switch FROM role_universe").fetchall()
    except Exception:
        log.exception("roles registry unreadable - observe stays static")
        return None
    if not role_rows:
        return None
    return _observe_switches_from_rows(role_rows, universe_rows)
