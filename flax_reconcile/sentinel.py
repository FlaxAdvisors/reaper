"""Intentional-flap sentinel writer (Postgres replacement for reaper's
filesystem write_flap_sentinel). UPSERT keyed on (switch, port); set_at
refreshes via NOW(). Migration 004's trigger NOTIFYs 'intentional_flap' so
flax-observe wakes. MUST be called BEFORE the flap."""
from psycopg_pool import ConnectionPool

_SQL = """
INSERT INTO intentional_flap (switch, port, hold_seconds, reason, mac, set_at)
VALUES (%(switch)s, %(port)s, %(hold)s, %(reason)s, %(mac)s, NOW())
ON CONFLICT (switch, port) DO UPDATE SET
    hold_seconds = EXCLUDED.hold_seconds,
    reason       = EXCLUDED.reason,
    mac          = EXCLUDED.mac,
    set_at       = NOW()
"""


def write_sentinel(pool: ConnectionPool, *, switch: str, port: str,
                   hold_seconds: int, reason: str, mac: "str | None") -> None:
    with pool.connection() as conn:
        conn.execute(_SQL, {"switch": switch, "port": port,
                            "hold": int(hold_seconds), "reason": reason,
                            "mac": mac})
