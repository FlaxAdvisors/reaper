"""Append-only reconcile_actions writer (table from migration 003)."""
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

_SQL = ("INSERT INTO reconcile_actions (switch, port, action, detail, outcome, reason) "
        "VALUES (%(switch)s, %(port)s, %(action)s, %(detail)s, %(outcome)s, %(reason)s)")


def log_action(pool: ConnectionPool, *, switch: str, port: str, action: str,
               detail: dict, outcome: str, reason: str | None = None) -> None:
    with pool.connection() as conn:
        conn.execute(_SQL, {"switch": switch, "port": port, "action": action,
                            "detail": Jsonb(detail), "outcome": outcome,
                            "reason": reason})


def write_ack(pool, consumer, source, generation, action, detail=None):
    """Upsert the consumer_acks high-water-mark for (consumer, source)."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    sql = (
        "INSERT INTO consumer_acks "
        "(consumer, source, generation, action, consumed_at, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (consumer, source) DO UPDATE SET "
        "generation=GREATEST(consumer_acks.generation, EXCLUDED.generation), "
        "action=EXCLUDED.action, consumed_at=EXCLUDED.consumed_at, detail=EXCLUDED.detail"
    )
    with pool.connection() as conn:
        conn.execute(sql, (consumer, source, int(generation), action, now, detail))
