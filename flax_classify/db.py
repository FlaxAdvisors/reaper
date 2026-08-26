"""Postgres read-side I/O for flax-classify.

Read-only snapshots of observe_state and switch_facts for the feeder.
The writer concern (publishing classified hosts) lives in
flax_classify.kea_hosts as of Plan 5; this module no longer touches
classify_proposals (dropped by migration 009).
"""
from psycopg_pool import ConnectionPool


def build_pool(conninfo: str, min_size: int = 1, max_size: int = 5) -> ConnectionPool:
    """Build a psycopg ConnectionPool for flax-classify. Mirrors the
    pattern in flax_switch_sense.db.build_pool / flax_observe.persistence."""
    pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size,
                          kwargs={"autocommit": True}, open=True)
    pool.wait()  # surface DSN/auth errors at process start, not first query
    return pool


_READ_OBSERVE_SQL = """
SELECT switch, port, resolved
  FROM observe_state
"""


def read_observe_rows(pool: ConnectionPool) -> list[dict]:
    """Snapshot of observe_state for the feeder. Returns row dicts."""
    with pool.connection() as conn:
        cur = conn.execute(_READ_OBSERVE_SQL)
        return [{"switch": s, "port": p, "resolved": r or {}}
                for s, p, r in cur.fetchall()]


_READ_SWITCH_FACTS_SQL = """
SELECT switch, ports
  FROM switch_facts
"""


def read_switch_facts(pool: ConnectionPool) -> dict:
    """Snapshot of switch_facts for the feeder. Returns
    {switch: {"ports": {port: {...}}}}.
    """
    with pool.connection() as conn:
        cur = conn.execute(_READ_SWITCH_FACTS_SQL)
        return {s: {"ports": p} for s, p in cur.fetchall()}


_READ_DEVICES_SQL = """
SELECT mac, switch, port, kind, latched
  FROM devices
"""


def read_post_order(pool):
    """Active post order_no from the post_settings singleton (id=1); None if
    unset or the row is missing. Requires GRANT SELECT on post_settings (migration
    027)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT order_no FROM post_settings WHERE id = 1").fetchone()
    return row[0] if row and row[0] else None


def read_devices(pool: ConnectionPool) -> list[dict]:
    """devices snapshot for the feeder family/vm_n join. Returns row dicts
    (latched NULL-guarded to {})."""
    with pool.connection() as conn:
        cur = conn.execute(_READ_DEVICES_SQL)
        return [{"mac": m, "switch": s, "port": p, "kind": k,
                 "latched": la or {}}
                for m, s, p, k, la in cur.fetchall()]


def db_now(pool):
    """Postgres clock (tz-aware) — the single time source for post reconcile."""
    with pool.connection() as conn:
        return conn.execute("SELECT now()").fetchone()[0]
