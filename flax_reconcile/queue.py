"""reconcile_requests work-queue ops: enqueue (dedup), claim, complete, defer.

Dedup is enforced by the partial unique index reconcile_requests_open_mac_idx
(migration 015): at most one pending|claimed row per mac. enqueue swallows the
unique violation. defer implements the cooldown + attempt-cap backoff (spec §7
step 5); at max_attempts a row is marked 'stuck' instead of retried forever.
"""
import logging

import psycopg
from psycopg_pool import ConnectionPool

log = logging.getLogger("flax-reconcile.queue")


def enqueue(pool: ConnectionPool, *, mac, requested_by, reason,
            switch=None, port=None, kind=None) -> bool:
    """Insert a pending request. Returns False if an open one already exists."""
    try:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO reconcile_requests "
                "(requested_by, mac, switch, port, kind, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (requested_by, mac, switch, port, kind, reason))
        return True
    except psycopg.errors.UniqueViolation:
        return False


def claim_next(pool: ConnectionPool) -> dict | None:
    """Atomically claim the oldest eligible pending row. None if queue empty.
    SKIP LOCKED keeps a future second worker safe (today single-writer)."""
    with pool.connection() as conn:
        row = conn.execute(
            "UPDATE reconcile_requests SET status='claimed', claimed_at=NOW() "
            "WHERE id = (SELECT id FROM reconcile_requests "
            "            WHERE status='pending' AND next_eligible <= NOW() "
            "            ORDER BY ts LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, mac, switch, port, kind, reason, attempts, status"
        ).fetchone()
    if not row:
        return None
    keys = ("id", "mac", "switch", "port", "kind", "reason", "attempts", "status")
    return dict(zip(keys, row))


def complete(pool: ConnectionPool, req_id: int, *, outcome: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE reconcile_requests SET status='done', outcome=%s, "
            "completed_at=NOW() WHERE id=%s", (outcome, req_id))


def reclaim_stale_claims(pool: ConnectionPool, *, older_than_secs: int) -> int:
    """Re-pend 'claimed' rows whose claim is older than older_than_secs.

    A claim is only legitimately held for the duration of ONE synchronous
    ladder execution (seconds, up to ~the bmc_ll probe interval). If the
    process dies between claim_next (sets status='claimed', claimed_at=NOW())
    and complete/defer (crash, systemctl restart, SIGKILL), the row stays
    'claimed' forever: the open-mac unique index (status IN pending|claimed)
    then blocks re-enqueue of that mac AND claim_next only claims 'pending',
    so the device is stranded. This is the queue analog of the interrupted-flap
    port self-heal in selfheal.py: reset stale claims back to 'pending' so the
    next cycle re-processes them. Uses the DB clock (NOW()). Returns rowcount.

    Only 'claimed' rows are reclaimed -- 'stuck' rows legitimately hit
    max_attempts and must NOT be revived here.
    """
    with pool.connection() as conn:
        cur = conn.execute(
            "UPDATE reconcile_requests "
            "   SET status = 'pending', claimed_at = NULL "
            " WHERE status = 'claimed' "
            "   AND claimed_at < NOW() - make_interval(secs => %s)",
            (older_than_secs,))
        return cur.rowcount


def defer(pool: ConnectionPool, req_id: int, *, cooldown_secs: int,
          max_attempts: int) -> None:
    """A kick attempt failed: bump attempts; either reschedule after the
    cooldown or, at the cap, mark the request 'stuck'."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE reconcile_requests SET "
            "  attempts = attempts + 1, "
            "  status = CASE WHEN attempts + 1 >= %(cap)s THEN 'stuck' ELSE 'pending' END, "
            "  outcome = CASE WHEN attempts + 1 >= %(cap)s THEN 'stuck' ELSE outcome END, "
            "  next_eligible = NOW() + (%(cd)s || ' seconds')::interval "
            "WHERE id = %(id)s",
            {"cap": max_attempts, "cd": cooldown_secs, "id": req_id})
