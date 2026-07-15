"""Read-side I/O + pool for flax-reconcile. Mirrors flax_discover.db.

IP rendering mirrors flax_observe.host_probe.lookup_kea_ip:
  host(('0.0.0.0'::inet) + address)  -- Kea stores v4 addresses as int4.
MAC rendering: Kea stores hwaddr/dhcp_identifier as bytea; we render canonical
colon-lowercase via regexp_replace(encode(col, 'hex'), ...).

The _MAC_SQL template uses a regular Python string (not an f-string) and doubled
backslashes so the rendered SQL contains E'\\1:\\2:...' -- the PostgreSQL E-string
escape for backreferences.  Py3.11-safe: no backslashes inside f-string braces.
"""
import logging

from psycopg_pool import ConnectionPool

from .portname import to_arista

log = logging.getLogger("flax-reconcile.db")


def build_pool(conninfo: str, min_size: int = 1, max_size: int = 5) -> ConnectionPool:
    pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size,
                          kwargs={"autocommit": True}, open=True)
    pool.wait()
    return pool


# Renders a bytea column to canonical colon-lowercase MAC, e.g. "aa:bb:cc:00:00:01".
# %s is replaced by the column name via Python string formatting (not psycopg param).
# The E'\\1:\\2:...' is the PostgreSQL escape-string literal for backreferences.
_MAC_SQL = ("regexp_replace(encode(%s, 'hex'), "
            "'(..)(..)(..)(..)(..)(..)', E'\\\\1:\\\\2:\\\\3:\\\\4:\\\\5:\\\\6')")


def _mac_hex(mac: str) -> str:
    """1c:34:da:7f:b3:a4 -> 1c34da7fb3a4 (lower); matches flax_classify._mac_hex.

    Used with bytes.fromhex(...) to bind a mac string to a kea bytea column
    (kea.lease4.hwaddr / kea.hosts.dhcp_identifier) as a psycopg3 param.
    """
    return mac.replace(":", "").replace("-", "").replace(".", "").lower()


def read_active_leases(pool: ConnectionPool) -> list[dict]:
    """[{mac, ip}] from kea.lease4 where state=0 (active)."""
    sql = (
        "SELECT " + (_MAC_SQL % "hwaddr") + " AS mac, "
        "host(('0.0.0.0'::inet) + address) AS ip "
        "FROM kea.lease4 WHERE state = 0"
    )
    with pool.connection() as conn:
        return [{"mac": m, "ip": ip} for m, ip in conn.execute(sql).fetchall()]


def lease_ip_for_mac(pool: ConnectionPool, mac: str) -> str | None:
    """Return the active (state=0) Kea lease IP for `mac`, or None.

    Used by the operator BMC-reset path to resolve the BMC's current IP from
    its MAC -- the reconcile_requests row carries the bmc MAC, and the Redfish
    reset targets the BMC at its leased address. Matches on the bytea hwaddr
    rendered to canonical colon-lowercase via _MAC_SQL (case-insensitive).
    Newest lease wins if more than one row exists for the hwaddr.
    """
    sql = (
        "SELECT host(('0.0.0.0'::inet) + address) AS ip "
        "FROM kea.lease4 "
        "WHERE state = 0 AND lower(" + (_MAC_SQL % "hwaddr") + ") = lower(%s) "
        "ORDER BY expire DESC NULLS LAST LIMIT 1"
    )
    with pool.connection() as conn:
        row = conn.execute(sql, (mac,)).fetchone()
    return row[0] if row else None


def release_stale_lease(pool: ConnectionPool, mac: str) -> list:
    """Release the device's stale Kea lease(s) via the SECURITY DEFINER fn
    kea.flax_release_stale_lease (migration 019). The fn self-guards
    (l.address <> h.ipv4_address) so a converged lease is never deleted, runs
    as its postgres owner (flax_reconcile only has SELECT on kea.lease4), and
    SETs search_path=kea so the func_lease4_adel trigger's unqualified
    isJsonSupported() resolves. Returns the released IP strings. Best-effort:
    on any error (e.g. fn absent pre-migration) log + return [] so the kick
    still flaps."""
    mac_bytes = bytes.fromhex(_mac_hex(mac))
    try:
        with pool.connection() as conn:
            cur = conn.execute(
                "SELECT kea.flax_release_stale_lease(%s)", (mac_bytes,))
            return [r[0] for r in cur.fetchall() if r[0] is not None]
    except Exception as e:
        log.warning("release_stale_lease failed for %s: %s", mac, e)
        return []


def read_reservations(pool: ConnectionPool, eligible_sources=None) -> list[dict]:
    """[{mac, ip}] from kea.hosts with an ipv4 reservation (dhcp_identifier_type=0).

    Excludes user_context.source='post' rows (in both modes below): those are
    reservations the post workflow owns on a switch flax-reconcile does NOT
    observe (e.g. rabbit-edam). reconcile cannot resolve their port from
    observe_state, so its lease!=reservation convergence would release the v4
    lease and flap a mis-resolved (wrong-switch) port forever -- a flap-storm
    that the post node's wicked DHCP can never satisfy (it gives up after one
    failure). Post owns its own one-shot convergence (donum bounce); reconcile
    stays out. Symmetric to flax-classify's stale-sweep source=post guard.

    Two filter modes, selected by `eligible_sources`
    (role_caps.read_reconcile_eligible_sources's return value):

    * frozenset[str] -- the registry-driven mode. Filters to
      `COALESCE(source,'') = ANY(sources)`, the declared reconcile-eligible
      set (roles whose capabilities.reconcile_switch is true, plus the
      always-eligible legacy sources 'legacy-import' and ''). Under the LIVE
      registry (triage=true, post=false) this selects exactly the same rows
      as the legacy literal below.
    * None (default) -- the pre-registry fallback, used verbatim when the
      registry is empty/unpublished (deploy-order safety): the CURRENT
      `source <> 'post'` literal, unchanged from before this filter existed.
    """
    if eligible_sources is not None:
        sql = (
            "SELECT " + (_MAC_SQL % "dhcp_identifier") + " AS mac, "
            "host(('0.0.0.0'::inet) + ipv4_address) AS ip "
            "FROM kea.hosts "
            "WHERE dhcp_identifier_type = 0 AND ipv4_address IS NOT NULL "
            "AND ipv4_address <> 0 "
            "AND COALESCE((user_context::jsonb) ->> 'source', '') = ANY(%(sources)s)"
        )
        with pool.connection() as conn:
            rows = conn.execute(
                sql, {"sources": list(eligible_sources)}).fetchall()
            return [{"mac": m, "ip": ip} for m, ip in rows]

    sql = (
        "SELECT " + (_MAC_SQL % "dhcp_identifier") + " AS mac, "
        "host(('0.0.0.0'::inet) + ipv4_address) AS ip "
        "FROM kea.hosts "
        "WHERE dhcp_identifier_type = 0 AND ipv4_address IS NOT NULL "
        "AND ipv4_address <> 0 "
        "AND COALESCE((user_context::jsonb) ->> 'source', '') <> 'post'"
    )
    with pool.connection() as conn:
        return [{"mac": m, "ip": ip} for m, ip in conn.execute(sql).fetchall()]


def resolve_location(pool: ConnectionPool, mac: str) -> dict:
    """Look up {switch, port, kind} for a mac from the devices table; {} if absent.

    devices.port is written by flax-discover in internal short form (et6b1).
    The reconcile flow is Arista-canonical end to end (spec §6): the auto-kick
    flap, the intentional_flap sentinel, and the steered_ports skip comparison
    all use Arista names. Canonicalize here at the ingest boundary so every
    downstream consumer sees the same shape. to_arista is idempotent on
    already-canonical names and a passthrough for non-Arista names.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT switch, port, kind FROM devices WHERE lower(mac) = lower(%s)",
            (mac,)).fetchone()
    if not row:
        return {}
    return {"switch": row[0], "port": to_arista(row[1]), "kind": row[2]}


def read_desired_ports(pool: ConnectionPool) -> list[dict]:
    """[{switch, port, desired_vid, occupants}] from classify's desired_port table."""
    with pool.connection() as conn:
        cur = conn.execute("SELECT switch, port, desired_vid, occupants "
                           "FROM desired_port")
        return [{"switch": s, "port": p, "desired_vid": v, "occupants": o or {}}
                for s, p, v, o in cur.fetchall()]


def read_switch_facts_ports(pool: ConnectionPool) -> dict:
    """{(switch, port): {mask, access_vid, link}} from switch_facts.ports JSONB.

    Only includes reachable switches (WHERE reachable).
    mask is 'access'|'trunk' (omitted -> None); access_vid is int on access ports.
    link is 'link'|'nolink'|'unknown' from slice.py (omitted -> None).
    switch_facts.ports is keyed by the driver's canonical port name
    (e.g. Arista 'Ethernet6/1'); desired_port uses the same canonical name.
    """
    out = {}
    with pool.connection() as conn:
        cur = conn.execute("SELECT switch, ports FROM switch_facts WHERE reachable")
        for switch, ports in cur.fetchall():
            for port, fact in (ports or {}).items():
                out[(switch, port)] = {"mask": fact.get("mask"),
                                       "access_vid": fact.get("access_vid"),
                                       "link": fact.get("link")}
    return out


def read_installing_ports(pool: ConnectionPool) -> set:
    """Return {(switch, port)} for hosts actively mid-PXE-install.

    A host is "installing" when observe has seen it fetch the Live ISO
    (vars.nodepxe.value = 'found') but install has NOT completed
    (vars.inventory.value != 'found', including a missing/null inventory var via
    COALESCE). The reconcile cycle uses this to skip the no-lease kick for such a
    port: flapping a mid-install host interrupts the install so it never
    converges -> a flap-storm that the circuit-breaker only bounds, never avoids.

    observe_state.port is stored in the INTERNAL short form (et7b2), but the
    cycle compares against loc.get("port") which db.resolve_location returns as
    `to_arista(devices.port)` (Ethernet7/2). So we MUST apply to_arista here too
    or the gate would never match on Arista (rabbit-*) switches. to_arista is a
    passthrough for non-Arista names (turtle swp23 stays swp23).

    Best-effort: on any error (e.g. the migration-021 grant not yet applied, so
    the SELECT is permission-denied) log + return an EMPTY set. A missed gate is
    far cheaper than crashing the whole reconcile cycle.
    """
    sql = (
        "SELECT switch, port FROM observe_state "
        "WHERE vars->'nodepxe'->>'value' = 'found' "
        "AND COALESCE(vars->'inventory'->>'value', '') <> 'found'"
    )
    try:
        with pool.connection() as conn:
            return {(s, to_arista(p)) for s, p in conn.execute(sql).fetchall()}
    except Exception as e:
        log.warning("read_installing_ports failed; treating no port as "
                    "installing: %s", e)
        return set()


def db_now(pool: ConnectionPool):
    """Return the database clock (NOW()) once per cycle.

    The circuit-breaker gates on timestamps stored by record_flap (which uses
    the DB clock, NOW()), so comparing them against the DB clock -- not the
    Python clock -- keeps the predicate consistent regardless of any drift
    between the daemon host and Postgres. Read once per cycle and pass the same
    value to every flap_blocked() call so all gating decisions in one cycle
    share a single reference time.
    """
    with pool.connection() as conn:
        return conn.execute("SELECT NOW()").fetchone()[0]


def read_flap_state(pool: ConnectionPool) -> dict:
    """Return {mac: row} for every reconcile_flap_state row.

    row is a dict with the timestamp/counter fields the circuit-breaker reads:
    last_flap_at, flaps_in_window, window_start, backoff_until, faulted.
    """
    out = {}
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT mac, last_flap_at, flaps_in_window, window_start, "
            "backoff_until, faulted FROM reconcile_flap_state")
        for mac, last_flap_at, flaps, window_start, backoff_until, faulted in cur.fetchall():
            out[mac] = {"mac": mac, "last_flap_at": last_flap_at,
                        "flaps_in_window": flaps, "window_start": window_start,
                        "backoff_until": backoff_until, "faulted": faulted}
    return out


def _flap_decision(flaps: int, was_faulted: bool, *, threshold: int) -> tuple[bool, bool]:
    """Pure circuit-breaker decision over the post-increment counter state.

    Given the post-increment `flaps_in_window` and the MAC's prior `faulted`
    flag, decide:

      * rearm  -- whether to (re-)arm the backoff window + set faulted=TRUE.
        TRUE on EVERY threshold hit (idempotent on an already-faulted MAC) so a
        never-converging port settles into a bounded low-frequency retry
        (~threshold flaps, then backoff_secs quiet, repeat) instead of reverting
        to a kick_cooldown_secs flap cadence forever once the first backoff
        window expires.
      * newly  -- whether to RETURN True so the caller emits the audit fault.
        TRUE only on the false->true transition (threshold reached AND the MAC
        was not already faulted) so the fault is logged exactly once per
        circuit-open, never re-spammed on subsequent re-arms.

    Kept SQL-free so the re-arm boundary is exhaustively unit-testable DB-free.
    """
    rearm = flaps >= threshold
    newly = rearm and not was_faulted
    return rearm, newly


def record_flap(pool: ConnectionPool, mac: str, *, threshold: int,
                window_secs: int, backoff_secs: int) -> bool:
    """UPSERT the per-MAC flap counter using the DB clock for all time math.

    On conflict: if the window has expired (window_start IS NULL, or NOW() -
    window_start exceeds window_secs) reset the window (window_start=NOW(),
    flaps_in_window=1); otherwise increment flaps_in_window. Always stamp
    last_flap_at/updated_at=NOW(). If the resulting flaps_in_window reaches
    threshold, (re-)open the circuit: backoff_until = NOW() + backoff_secs,
    faulted = true -- on EVERY threshold hit, not just the first, so a
    never-converging MAC keeps backing off instead of resuming a fast flap
    cadence once the initial backoff expires.

    Returns True iff this call NEWLY faulted the MAC (was not faulted before,
    is now) so the caller emits exactly one audit fault per circuit-open. A
    re-arm of an already-faulted MAC returns False (no fault spam).
    """
    sql = (
        "INSERT INTO reconcile_flap_state "
        "  (mac, last_flap_at, flaps_in_window, window_start, faulted, updated_at) "
        "VALUES (%(mac)s, NOW(), 1, NOW(), FALSE, NOW()) "
        "ON CONFLICT (mac) DO UPDATE SET "
        "  window_start = CASE "
        "    WHEN reconcile_flap_state.window_start IS NULL "
        "      OR NOW() - reconcile_flap_state.window_start "
        "         > make_interval(secs => %(window)s) "
        "    THEN NOW() ELSE reconcile_flap_state.window_start END, "
        "  flaps_in_window = CASE "
        "    WHEN reconcile_flap_state.window_start IS NULL "
        "      OR NOW() - reconcile_flap_state.window_start "
        "         > make_interval(secs => %(window)s) "
        "    THEN 1 ELSE reconcile_flap_state.flaps_in_window + 1 END, "
        "  last_flap_at = NOW(), "
        "  updated_at = NOW() "
        "RETURNING flaps_in_window, faulted"
    )
    upd = (
        "UPDATE reconcile_flap_state SET "
        "  backoff_until = NOW() + make_interval(secs => %(backoff)s), "
        "  faulted = TRUE, updated_at = NOW() "
        "WHERE mac = %(mac)s RETURNING TRUE"
    )
    with pool.connection() as conn:
        with conn.transaction():
            flaps, was_faulted = conn.execute(
                sql, {"mac": mac, "window": window_secs}).fetchone()
            rearm, newly = _flap_decision(flaps, was_faulted, threshold=threshold)
            if rearm:
                conn.execute(upd, {"mac": mac, "backoff": backoff_secs})
            return newly
    return False


def clear_flap_state(pool: ConnectionPool, macs: list) -> None:
    """Delete flap-state rows for the given MACs (converged -> circuit closes).

    psycopg3 list-membership form (= ANY); no-op on an empty list.
    """
    if not macs:
        return
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM reconcile_flap_state WHERE mac = ANY(%s)", (macs,))


def clear_stale_flap_state(pool: ConnectionPool, *, older_than_secs: int) -> None:
    """Time-based GC of flap_state rows whose last_flap_at has aged out.

    Replaces the old mismatch-membership clear (which wiped a per-port
    flap-pong MAC's cooldown the instant it momentarily converged, letting the
    port re-kick every cycle and never accumulate to backoff). A MAC that
    genuinely converges simply stops being flapped, so its last_flap_at ages
    past older_than_secs and the row is dropped; a flap-pong MAC keeps getting
    record_flap'd so last_flap_at stays fresh and it survives to accumulate.

    older_than_secs is the backoff window: a MAC in backoff is gated (not
    flapped) so its last_flap_at sits at backoff-start; once backoff_secs
    elapses the row both un-gates (backoff_until passed) AND ages out -- so a
    still-mismatching MAC gets a fresh row and re-accumulates (the intended
    re-arm), while a converged MAC's row disappears. Uses the DB clock (NOW())
    so it shares the reference time of record_flap's stamps; psycopg3 param via
    make_interval. A NULL last_flap_at row (none should exist post-insert) is
    also reaped.
    """
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM reconcile_flap_state "
            "WHERE last_flap_at IS NULL "
            "OR last_flap_at < NOW() - make_interval(secs => %s)",
            (older_than_secs,))


def flap_blocked(row: dict, now, kick_cooldown_secs: int) -> bool:
    """Pure predicate: is this MAC currently circuit-gated against re-enqueue?

    True when EITHER the backoff window is still open (now < backoff_until) OR
    we are still inside the cooldown-on-success window
    (now < last_flap_at + kick_cooldown_secs). `now` is the DB clock from
    db_now(pool); all three timestamps share that reference. Kept SQL-free so it
    is exhaustively unit-testable with crafted rows + timestamps.
    """
    import datetime
    backoff_until = row.get("backoff_until")
    if backoff_until is not None and now < backoff_until:
        return True
    last_flap_at = row.get("last_flap_at")
    if last_flap_at is not None:
        if now < last_flap_at + datetime.timedelta(seconds=kick_cooldown_secs):
            return True
    return False


def mark_flap_pending(pool: ConnectionPool, *, switch: str, port: str,
                      mac: "str | None") -> None:
    """Record (switch, port, mac) as a flap in-flight, BEFORE the shutdown.

    A row left behind at the next startup (or at SIGTERM with a flap mid-flight)
    marks a port reconcile was shutting down but may not have brought back up --
    the precise self-heal signal. UPSERT keyed on (switch, port) so a re-flap of
    the same port refreshes set_at + mac.
    """
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO reconcile_flap_pending (switch, port, mac, set_at) "
            "VALUES (%s, %s, %s, NOW()) "
            "ON CONFLICT (switch, port) DO UPDATE SET "
            "  mac = EXCLUDED.mac, set_at = NOW()",
            (switch, port, mac))


def clear_flap_pending(pool: ConnectionPool, *, switch: str, port: str) -> None:
    """Delete the flap-pending marker after the no-shutdown completes."""
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM reconcile_flap_pending WHERE switch = %s AND port = %s",
            (switch, port))


def read_flap_pending(pool: ConnectionPool) -> list[dict]:
    """Return [{switch, port, mac}] for every outstanding flap-pending marker.

    Any row here was written before a shutdown whose matching no-shutdown never
    cleared it (process died mid-flap) -- the precise set of ports the self-heal
    must un-strand.
    """
    out = []
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT switch, port, mac FROM reconcile_flap_pending")
        for switch, port, mac in cur.fetchall():
            out.append({"switch": switch, "port": port, "mac": mac})
    return out


def port_admin_down(fact: dict | None) -> bool:
    """Best-effort: True iff the switch_facts port fact shows link='nolink'.

    'nolink' is the value slice.py records for an admin-down/link-down port, so
    this returns True ONLY for that explicit state. A port whose link reads
    'unknown' or None (never polled / stale switch_facts) is NOT treated as
    admin-down: returning True there would let the fallback self-heal scan
    `no shutdown` a port an operator intentionally disabled, or an un-polled
    port whose true state we don't know.

    NOTE: the persisted switch_facts.ports[*].link field collapses Arista
    'disabled' (admin-down) into 'nolink' together with a genuinely absent
    device ('notconnect'/'down'). So this still CANNOT distinguish a reconcile-
    stranded admin-down port from an empty (but polled) port on its own -- the
    flap-pending marker is the precise signal. This predicate is only used as the
    conservative fallback scan (re-issuing a harmless `no shutdown` on a managed
    access DUT port that reads 'nolink'; a no-op on a port that is already up or
    genuinely empty). A trunk/uplink or non-managed port is excluded by the
    caller.
    """
    if fact is None:
        return False
    return fact.get("link") == "nolink"


def port_link_up(fact: dict | None) -> bool:
    """Return True iff the switch_facts port fact shows link='link' (link-up).

    fact is a {mask, access_vid, link} dict from read_switch_facts_ports, or
    None when the port is not in switch_facts at all (switch unreachable or port
    not yet polled).  Any value other than 'link' (including 'nolink', 'unknown',
    and missing) is treated as not-up, so we never kick an absent device.
    """
    if fact is None:
        return False
    return fact.get("link") == "link"
