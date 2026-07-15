"""Write-side I/O for flax-discover: latch-aware devices upsert + vm_n.

Latch rules (see spec 2026-06-15-flax-discover-design.md §4.2):
  - family/serial/product_name: write-once. Once a real family is latched it is
    never re-derived. The cycle computes the final `latched` dict (preserving an
    already-known family) and passes it here; this module just writes it.
  - last_seen + location (switch/port) always refresh.
  - generation bumps ONLY when latched, location (switch/port), or kind changed,
    so it stays in lockstep with the guarded NOTIFY trigger (migration 014),
    which fires on exactly those changes (latched/switch/port/kind).
"""
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


def next_vm_n(existing: dict) -> int:
    """Lowest free vm_n given {mac: vm_n} of VMs already on this port.
    Mirrors reaper_leased._next_vm_n's behaviour: lowest free slot (gap-fill)."""
    used = set(existing.values())
    n = 1
    while n in used:
        n += 1
    return n


_UPSERT_SQL = """
INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation)
VALUES (%(mac)s, %(switch)s, %(port)s, %(kind)s, %(latched)s, NOW(), 1)
ON CONFLICT (mac) DO UPDATE SET
    switch     = EXCLUDED.switch,
    port       = EXCLUDED.port,
    kind       = EXCLUDED.kind,
    latched    = EXCLUDED.latched,
    last_seen  = NOW(),
    updated_at = NOW(),
    generation = devices.generation + (
        CASE WHEN devices.latched IS DISTINCT FROM EXCLUDED.latched
              OR devices.switch  IS DISTINCT FROM EXCLUDED.switch
              OR devices.port    IS DISTINCT FROM EXCLUDED.port
              OR devices.kind    IS DISTINCT FROM EXCLUDED.kind
             THEN 1 ELSE 0 END)
"""


def upsert_device(pool: ConnectionPool, *, mac: str, switch: str, port: str,
                  kind: str, latched: dict) -> None:
    with pool.connection() as conn:
        conn.execute(_UPSERT_SQL, {
            "mac": mac, "switch": switch, "port": port, "kind": kind,
            "latched": Jsonb(latched),
        })


# "One BMC per (switch, port)" supersede. When a DIFFERENT BMC arrives at a
# port than the one we last recorded, the prior occupant's chassis is gone:
# delete every devices row at that (switch, port) whose mac is NOT part of the
# just-arrived chassis (new bmc + paired host nic + this cycle's VMs).
#
# psycopg3 binds a list/tuple param as a single $1 array, so `NOT IN %s`
# becomes the invalid `NOT IN $1`. Use `<> ALL(%s)` with a LIST (rendered as a
# Postgres array) -- mirrors the flax_classify.kea_hosts <> ALL(%s) fix. The
# psycopg2 `IN %s` tuple-expansion idiom does NOT carry over. keep_macs must be
# a list. An empty list yields `mac <> ALL('{}')` which is TRUE for every row
# (deletes all at the port); callers always pass at least the new bmc mac.
_SUPERSEDE_SQL = """
DELETE FROM devices
 WHERE switch = %(switch)s
   AND port = %(port)s
   AND mac <> ALL(%(keep)s)
"""


def supersede_port(pool: ConnectionPool, *, switch: str, port: str,
                   keep_macs: list[str]) -> int:
    """Delete devices rows at (switch, port) whose mac is not in keep_macs.

    keep_macs MUST be a list (psycopg3 array param) and SHOULD already be
    normalised. Returns the number of rows deleted. The caller is responsible
    for the BMC-change gate -- this helper unconditionally evicts non-kept
    rows at the port."""
    with pool.connection() as conn:
        cur = conn.execute(_SUPERSEDE_SQL, {
            "switch": switch, "port": port, "keep": list(keep_macs),
        })
        return cur.rowcount


# Vacancy sweep. A port that has gone link-down (caller checks switch_facts)
# stops having its MACs seen, so their last_seen freezes. Once the NEWEST row
# at the port is older than the debounce, the chassis is gone -- delete every
# devices row at that (switch, port). The age test uses the DB clock (now()),
# NOT the daemon clock, and the max(last_seen) subquery means one still-fresh
# MAC keeps the whole port. Self-guard: a port with no rows yields max()=NULL,
# `NULL < ...` is NULL, so nothing is deleted.
_SWEEP_VACANT_SQL = """
DELETE FROM devices
 WHERE switch = %(switch)s
   AND port = %(port)s
   AND (SELECT max(last_seen) FROM devices d2
         WHERE d2.switch = %(switch)s AND d2.port = %(port)s)
       < now() - make_interval(secs => %(debounce)s)
"""


def sweep_vacant_port(pool: ConnectionPool, *, switch: str, port: str,
                      debounce_secs: float) -> int:
    """Delete ALL devices rows at (switch, port) iff the most-recently-seen row
    there is older than debounce_secs -- i.e. the port has been vacant past the
    debounce. The age test uses the DB clock (now()), so it is independent of
    the daemon-host clock. The CALLER is responsible for the link=='nolink'
    gate; this helper enforces only the debounce. Returns rows deleted."""
    with pool.connection() as conn:
        cur = conn.execute(_SWEEP_VACANT_SQL, {
            "switch": switch, "port": port, "debounce": debounce_secs,
        })
        return cur.rowcount


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
