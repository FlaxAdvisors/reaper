"""Operator-initiated flap enqueue. Mirrors operator_notes' write pattern;
flax_control holds INSERT on reconcile_requests (migration 015). The actual
switch write happens in flax-reconcile on the MASTER -- this only enqueues."""
import psycopg
from .db import get_pool


def enqueue_flap(*, mac: str, switch: str, port: str, kind: str | None,
                 operator: str) -> bool:
    """Insert an operator flap request. Returns False if an open request for
    this mac already exists (the open-mac unique index absorbs the dup)."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO reconcile_requests "
                "(requested_by, mac, switch, port, kind, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("op:" + operator, mac, switch, port, kind, "operator flap"))
        return True
    except psycopg.errors.UniqueViolation:
        return False


def enqueue_bmc_reset(*, mac: str, switch: str, port: str, kind: str | None,
                      operator: str) -> bool:
    """Insert an operator BMC-reset request (reason='operator bmc-reset').

    Mirrors enqueue_flap: flax_control only enqueues; flax-reconcile on the
    MASTER drains reconcile_requests and dispatches this reason to its Redfish
    Manager.Reset path (NOT the kick/steer ladder). Returns False if an open
    request for this mac already exists (the open-mac unique index absorbs the
    dup)."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO reconcile_requests "
                "(requested_by, mac, switch, port, kind, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("op:" + operator, mac, switch, port, kind, "operator bmc-reset"))
        return True
    except psycopg.errors.UniqueViolation:
        return False
