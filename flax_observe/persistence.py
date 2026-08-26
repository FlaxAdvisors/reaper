"""observe_state + audit.events writers for flax-observe."""
import json
import logging

from .db import get_pool


log = logging.getLogger("flax-observe.persistence")


def upsert_observe_state(*, switch: str, port: str, vars: dict,
                          last_polled: str, resolved: dict | None = None) -> int:
    """UPSERT one observe_state row, atomically incrementing generation.

    `resolved` carries the scalar fields the state machine resolved this
    cycle (bmc_mac, bmc_ip, nic_mac, nic_ip, chassis_sn, bmc_power) so
    triage_compat can render real values instead of state-machine flags.
    """
    sql = """
        INSERT INTO observe_state (switch, port, vars, last_polled, generation, resolved)
        VALUES (%(switch)s, %(port)s, %(vars)s, %(last_polled)s, 1, %(resolved)s)
        ON CONFLICT (switch, port) DO UPDATE SET
          vars        = EXCLUDED.vars,
          last_polled = EXCLUDED.last_polled,
          resolved    = EXCLUDED.resolved,
          generation  = observe_state.generation + 1
        RETURNING generation
    """
    params = {
        "switch": switch, "port": port,
        "vars": json.dumps(vars),
        "last_polled": last_polled,
        "resolved": json.dumps(resolved or {}),
    }
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            (gen,) = cur.fetchone()
    return gen


def emit_audit_event(*, kind: str, switch: str | None, port: str | None,
                      mac: str | None, payload: dict) -> None:
    """Append one row to audit.events."""
    sql = """
        INSERT INTO audit.events (service, kind, mac, switch, port, payload)
        VALUES ('flax-observe', %(kind)s, %(mac)s, %(switch)s, %(port)s, %(payload)s)
    """
    params = {
        "kind": kind, "mac": mac, "switch": switch, "port": port,
        "payload": json.dumps(payload),
    }
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def read_prior_observe_state(pool) -> dict:
    """Batch-read every observe_state row for boot hydration.

    Returns {(switch, port): {"vars": <dict>, "resolved": <dict>}}. `vars` and
    `resolved` are JSONB columns psycopg3 already decodes to dicts; a NULL/empty
    `resolved` coalesces to {}. One SELECT for the whole fleet -- called once at
    startup, not per worker.
    """
    sql = "SELECT switch, port, vars, resolved FROM observe_state"
    out: dict = {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            for switch, port, vars_, resolved in cur.fetchall():
                out[(switch, port)] = {"vars": vars_, "resolved": resolved or {}}
    return out


def read_active_sentinels(pool, *, grace_secs: int) -> set:
    """Return {(switch, port)} for intentional_flap rows still inside their
    freeze window: NOW() <= set_at + hold_seconds + grace. flax_observe holds
    SELECT on intentional_flap (migration 003)."""
    sql = ("SELECT switch, port FROM intentional_flap "
           "WHERE NOW() <= set_at + ((hold_seconds + %s) || ' seconds')::interval")
    with pool.connection() as conn:
        return {(s, p) for s, p in conn.execute(sql, (grace_secs,)).fetchall()}


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


def ack_switch_facts(*, generation: int, action: str, detail: str | None = None) -> None:
    """Update consumer_acks to say we have consumed switch_facts up to `generation`.

    Thin shim over the shared-shape write_ack helper."""
    write_ack(get_pool(), "flax-observe", "switch_facts", generation, action, detail)
