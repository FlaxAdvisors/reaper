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


_SWEEP_SQL = """
DELETE FROM desired_port
WHERE switch = ANY(%(switches)s)
  AND NOT (switch || '|' || port = ANY(%(keep)s))
"""


def sweep_desired_ports_not_in(pool: ConnectionPool, *, keep, switches) -> int:
    """Delete the desired_port rows of `switches` whose (switch, port) is not in
    `keep` (this cycle's writes). Returns the number deleted.

    A port classify stops writing (its occupant went quiet, it joined no-steer,
    it left every role) must not keep its last desired_vid: reconcile enforces a
    row as long as it exists (2026-11-18, rabbit-fiesta Ethernet28/3 was set back
    to a dead VLAN from a row frozen 15 minutes earlier). Only switches with
    facts this cycle are swept, so a missed poll never clears a switch.
    """
    switches = sorted(switches)
    if not switches:
        return 0
    keys = sorted(f"{sw}|{port}" for sw, port in keep)
    with pool.connection() as conn:
        return conn.execute(_SWEEP_SQL, {"switches": switches, "keep": keys}).rowcount


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
