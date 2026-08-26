"""desired_reservations writer + mac_ownership_events ledger.

Shadow materialization phase 2 (Task 2): a per-role "desired" row for every
mac an owning lane (triage, post, ...) currently claims, plus an append-only
ledger of ownership handoffs (e.g. triage -> post when a chassis is
repurposed). This module is SHADOW ONLY -- it never touches kea.hosts /
kea.ipv6_reservations; the materializer (a later task) is the only thing
that may read this table and reconcile it into materializer_plan.

Table shapes: migration 030 (schema/versions/030_desired_reservations.py).
Transaction pattern: flax_reconcile/db.py (record_flap, `with
pool.connection() as conn, conn.transaction():`). jsonb write idiom
(plain json.dumps into %s): flax_switch_sense/publisher.py.
"""
import json

from psycopg_pool import ConnectionPool


def _norm_mac(s: str) -> str:
    """Canonicalize a MAC to lowercase colon-hex (aa:bb:cc:dd:ee:ff).

    Mirrors flax_classify.post_reserve._norm_mac (same convention, kept
    local so this module has no cross-lane import).
    """
    h = "".join(c for c in s.lower() if c in "0123456789abcdef")
    if len(h) != 12:
        raise ValueError(f"bad MAC: {s!r}")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


_SELECT_OWNER_FOR_UPDATE = """
    SELECT owner_role FROM desired_reservations WHERE mac = %(mac)s FOR UPDATE
"""

_INSERT_LEDGER = """
    INSERT INTO mac_ownership_events (mac, from_role, to_role, switch, port)
    VALUES (%(mac)s, %(from_role)s, %(to_role)s, %(switch)s, %(port)s)
"""

_UPSERT = """
    INSERT INTO desired_reservations
        (mac, owner_role, kind, hostname, ipv4, ipv6, vid, switch, port, attrs)
    VALUES (%(mac)s, %(owner_role)s, %(kind)s, %(hostname)s, %(ipv4)s,
            %(ipv6)s, %(vid)s, %(switch)s, %(port)s, %(attrs)s)
    ON CONFLICT (mac) DO UPDATE SET
        owner_role = EXCLUDED.owner_role, kind = EXCLUDED.kind,
        hostname = EXCLUDED.hostname, ipv4 = EXCLUDED.ipv4,
        ipv6 = EXCLUDED.ipv6, vid = EXCLUDED.vid,
        switch = EXCLUDED.switch, port = EXCLUDED.port,
        attrs = EXCLUDED.attrs,
        generation = desired_reservations.generation + 1,
        updated_at = now()
"""


def upsert_desired(pool: ConnectionPool, *, owner_role, mac, kind, hostname,
                    ipv4, ipv6, vid, switch, port, attrs=None) -> str | None:
    """INSERT or bump the desired_reservations row for `mac`.

    One transaction: lock any existing row (SELECT ... FOR UPDATE), and if it
    exists under a DIFFERENT owner_role, record the handoff in
    mac_ownership_events (from=old owner, to=new owner) BEFORE the upsert so
    the ledger always reflects the state that existed at handoff time. Then
    upsert the row (generation bumps on every write, desired_port
    convention). Returns the previous owner_role when a handoff happened,
    else None (first-ever write, or a same-owner re-upsert).
    """
    norm_mac = _norm_mac(mac)
    params = {
        "mac": norm_mac, "owner_role": owner_role, "kind": kind,
        "hostname": hostname, "ipv4": ipv4, "ipv6": ipv6, "vid": vid,
        "switch": switch, "port": port, "attrs": json.dumps(attrs or {}),
    }
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(_SELECT_OWNER_FOR_UPDATE, {"mac": norm_mac}).fetchone()
        previous_owner = row[0] if row else None
        if previous_owner is not None and previous_owner != owner_role:
            conn.execute(_INSERT_LEDGER, {
                "mac": norm_mac, "from_role": previous_owner,
                "to_role": owner_role, "switch": switch, "port": port,
            })
        conn.execute(_UPSERT, params)
    return previous_owner if previous_owner != owner_role else None


def delete_desired(pool: ConnectionPool, *, owner_role, macs) -> int:
    """Delete the listed macs owned by owner_role. Returns rows deleted."""
    norm_macs = [_norm_mac(m) for m in macs]
    if not norm_macs:
        return 0
    with pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM desired_reservations "
            "WHERE owner_role = %s AND mac = ANY(%s)",
            (owner_role, norm_macs),
        )
        return cur.rowcount


def sweep_desired_not_in(pool: ConnectionPool, *, owner_role, keep_macs) -> int:
    """Delete owner_role's rows whose mac is NOT in keep_macs.

    Scoped to owner_role -- mirrors delete_stale_kea_hosts' semantics for the
    triage lane, but a global sweep would wipe other lanes' rows, so this
    scopes the delete to the calling lane's own owner_role. Returns rows
    deleted.
    """
    norm_keep = [_norm_mac(m) for m in keep_macs]
    with pool.connection() as conn:
        if norm_keep:
            cur = conn.execute(
                "DELETE FROM desired_reservations "
                "WHERE owner_role = %s AND mac <> ALL(%s)",
                (owner_role, norm_keep),
            )
        else:
            cur = conn.execute(
                "DELETE FROM desired_reservations WHERE owner_role = %s",
                (owner_role,),
            )
        return cur.rowcount


def delete_desired_slot(pool: ConnectionPool, *, owner_role, switch, port,
                        kind, keep_mac) -> int:
    """Delete the (switch, port, kind) desired row(s) owned by owner_role
    whose mac is NOT keep_mac -- mirrors purge_superseded_slot_hosts for the
    post lane (the prior occupant of THIS exact slot, superseded by a
    different mac now claiming it). Returns rows deleted.
    """
    norm_keep = _norm_mac(keep_mac)
    with pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM desired_reservations "
            "WHERE owner_role = %s AND switch = %s AND port = %s "
            "AND kind = %s AND mac <> %s",
            (owner_role, switch, port, kind, norm_keep),
        )
        return cur.rowcount


_READ_ALL = """
    SELECT mac, owner_role, kind, hostname, ipv4, ipv6, vid, switch, port,
           attrs, generation, updated_at
      FROM desired_reservations
"""


def read_desired(pool: ConnectionPool) -> list:
    """Snapshot of every desired_reservations row as dicts (keys = column
    names), for the materializer."""
    with pool.connection() as conn:
        cur = conn.execute(_READ_ALL)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
