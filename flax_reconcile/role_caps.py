"""Registry capability lookup for flax-reconcile (spec 2026-07-03 spine
migration, phase 3b Task 1).

reconcile only wants to converge kea.hosts reservations for sources whose
switch it actually observes. That used to be a hardcoded literal --
`source <> 'post'` in db.read_reservations (see that docstring for the full
flap-storm rationale: rabbit-edam/post reservations live on a switch
reconcile never polls, and converging them anyway flaps a mis-resolved port
forever). The registry migration (029_role_registry) turns "does this
source's switch get reconciled" into a declared role capability
(``capabilities.reconcile_switch``) instead of a single hardcoded string, so
adding a new non-reconciled role no longer requires a reconcile code change.

read_reconcile_eligible_sources reads the `roles` table and returns the set
of user_context.source values whose role opts into reconcile_switch=true.
Two source values predate the registry and must ALWAYS keep reconciling
exactly as the old literal did, regardless of what's published:
'legacy-import' (kea.hosts rows imported before any source tagging existed)
and '' (untagged rows -- COALESCE(...,'') on a NULL/absent
user_context.source; no live rows have this shape any more, but a bare
COALESCE default must not accidentally become excluded). No role is ever
named either string, so folding them in is always safe and purely additive.

When the roles table is empty (registry not yet published -- deploy-order
safety during the migration) or the query fails, this returns None so the
caller (db.read_reservations) falls back to the legacy `source <> 'post'`
literal filter, unchanged.
"""
import logging

from psycopg_pool import ConnectionPool

log = logging.getLogger("flax-reconcile.role_caps")

# See the module docstring: sources that predate the registry and must
# always stay reconcile-eligible, independent of what's published.
_ALWAYS_ELIGIBLE = frozenset({"legacy-import", ""})

_EMPTY_WARNING = ("roles registry empty - reconcile falls back to legacy "
                  "source<>'post' filter")


def _eligible_from_rows(rows) -> frozenset:
    """Pure: rows is [(role, definition), ...] (definition a dict or None).

    A role is eligible when definition["capabilities"]["reconcile_switch"] is
    truthy. A role with no "capabilities" key, or a "capabilities" dict with
    no "reconcile_switch" key, is NOT eligible (opt-in, not opt-out) -- a
    role that never declares the capability must not silently start
    reconciling.
    """
    eligible = set()
    for role, definition in rows:
        caps = (definition or {}).get("capabilities") or {}
        if caps.get("reconcile_switch"):
            eligible.add(role)
    return frozenset(eligible | _ALWAYS_ELIGIBLE)


def read_reconcile_eligible_sources(pool: ConnectionPool):
    """SELECT role, definition FROM roles -> frozenset[str] | None.

    None means "fall back to the legacy filter" (empty/unreadable registry).
    """
    try:
        with pool.connection() as conn:
            rows = conn.execute("SELECT role, definition FROM roles").fetchall()
    except Exception:
        log.exception(_EMPTY_WARNING)
        return None
    if not rows:
        log.warning(_EMPTY_WARNING)
        return None
    return _eligible_from_rows(rows)
