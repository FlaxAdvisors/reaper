"""desired_port UPSERT (table from migration 002; flax_classify has write grant).

generation bumps on every write so the notify_change trigger (NEW.generation)
fires and flax-reconcile wakes.
"""
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

_SQL = """
INSERT INTO desired_port (switch, port, desired_vid, occupants, generation)
VALUES (%(switch)s, %(port)s, %(vid)s, %(occ)s, 1)
ON CONFLICT (switch, port) DO UPDATE SET
    desired_vid = EXCLUDED.desired_vid,
    occupants   = EXCLUDED.occupants,
    wrote_at    = NOW(),
    generation  = desired_port.generation + 1
"""


def upsert_desired_port(pool: ConnectionPool, *, switch, port, desired_vid,
                        occupants) -> None:
    """INSERT or bump the desired_port row for (switch, port).

    occupants is a dict — e.g. {"bmc": <mac>, "host": <mac>} or
    {"bmc": <mac>, "host": <mac>, "vms": [<mac>, ...]} — stored as JSONB.
    generation starts at 1 and increments on every conflict so the
    notify_change trigger fires and flax-reconcile wakes.
    """
    with pool.connection() as conn:
        conn.execute(_SQL, {"switch": switch, "port": port, "vid": desired_vid,
                            "occ": Jsonb(occupants)})


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
