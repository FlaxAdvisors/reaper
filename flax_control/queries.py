"""Parameterized SQL queries for flax-control views.

Each function returns plain Python dicts (or lists of dicts) suitable for
direct JSON response or Jinja template rendering. SQL strings live here,
not inline in app.py — keeps the route handlers focused on shape adaptation.

Timestamp columns are converted to ISO strings via _iso() so JSONResponse
can serialize them without a custom encoder.
"""
import datetime
import re
from typing import Any

from .db import get_pool


def _natural_port_key(port: str) -> tuple:
    """Natural sort key for switch ports.

    Rabbit (Arista) ports `Ethernet6/1` / `et6b1` -> (0, 6, 1, ""); turtle
    (Cumulus) ports `swp23` -> (1, 23, 0, ""); anything else -> (2, 0, 0, port).
    Numeric, so et2b1 sorts before et10b2 (lexicographic would reverse them).
    """
    m = re.match(r'^(?:Ethernet|et)(\d+)(?:/|b)(\d+)$', port, re.I)
    if m:
        return (0, int(m.group(1)), int(m.group(2)), "")
    m = re.match(r'^swp(\d+)$', port, re.I)
    if m:
        return (1, int(m.group(1)), 0, "")
    return (2, 0, 0, port)


def _iso(value: Any) -> Any:
    """Convert datetime values to ISO strings; pass other types through."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def switches() -> list[dict[str, Any]]:
    """Return one row per known switch with derived port count.

    Postgres doesn't ship a `jsonb_object_length` function; count the keys
    via a correlated subquery on jsonb_object_keys.
    """
    sql = """
        SELECT switch, driver, polled_at, reachable, generation,
               (SELECT count(*) FROM jsonb_object_keys(ports))::int AS port_count
        FROM switch_facts
        ORDER BY switch
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [
        {"switch": r[0], "driver": r[1], "polled_at": _iso(r[2]),
         "reachable": r[3], "generation": r[4], "port_count": r[5]}
        for r in rows
    ]


def ports_for_switch(switch: str) -> list[dict[str, Any]]:
    """Return per-port detail (flat dict per port) for one switch."""
    sql = """
        SELECT key AS port, value
        FROM switch_facts, jsonb_each(ports)
        WHERE switch = %(switch)s
        ORDER BY key
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"switch": switch})
            rows = cur.fetchall()
    return sorted(
        [
            {"port": port, **(value if isinstance(value, dict) else {})}
            for port, value in rows
        ],
        key=lambda p: _natural_port_key(p["port"]),
    )


def desired_ports_for_switch(switch: str) -> dict[str, "int | None"]:
    """{arista_port: desired_vid} for one switch, from desired_port."""
    sql = "SELECT port, desired_vid FROM desired_port WHERE switch=%(s)s"
    with get_pool().connection() as conn:
        rows = conn.execute(sql, {"s": switch}).fetchall()
    return {port: vid for port, vid in rows}


def switch_ports_all() -> list[tuple]:
    """Return flat (switch, port) pairs for every ACCESS port on every known
    switch — same switch_facts/jsonb_each join ports_for_switch() uses,
    without the per-switch WHERE. Access-only (operator directive
    2026-07-04): uplinks/trunks/Cpu are outside every role's DHCP/vid lens,
    so the roles_view.coverage() would-be-unassigned view must not list
    them."""
    sql = """
        SELECT switch, key AS port
        FROM switch_facts, jsonb_each(ports)
        WHERE value->>'mask' = 'access'
        ORDER BY switch, key
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def roles() -> list[tuple]:
    """Return raw (role, definition, generation, loaded_at) rows from the
    `roles` table for roles_view.build_registry(). May be empty when the
    registry has not been published yet."""
    sql = "SELECT role, definition, generation, loaded_at FROM roles ORDER BY role"
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def role_universe() -> list[tuple]:
    """Return raw (role, kind, switch, port) rows from `role_universe` for
    roles_view.build_registry()/coverage(). May be empty."""
    sql = "SELECT role, kind, switch, port FROM role_universe ORDER BY role, kind"
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def materializer_recent(limit: int = 500) -> list[tuple]:
    """Return raw (ts, owner_role, action, mac, detail) rows from
    `materializer_plan`, newest first, for shadow_view.build_shadow(). The
    shadow materializer (flax_classify.materializer, phase 2) writes one row
    per PLANNED action plus exactly one "summary" marker row per run (even
    when nothing was planned), so an empty result here unambiguously means
    the materializer has never run -- see shadow_view.build_shadow()."""
    sql = ("SELECT ts, owner_role, action, mac, detail FROM materializer_plan "
           "ORDER BY id DESC LIMIT %(limit)s")
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"limit": limit})
            return cur.fetchall()


def ownership_events_recent(limit: int = 200, mac: str | None = None) -> list[tuple]:
    """Return raw (at, mac, from_role, to_role, switch, port) rows from
    `mac_ownership_events`, newest first, for shadow_view.build_shadow().
    Optionally filtered to one mac."""
    sql = ("SELECT at, mac, from_role, to_role, switch, port "
           "FROM mac_ownership_events ")
    params: dict = {}
    if mac:
        sql += "WHERE mac = %(mac)s "
        params["mac"] = mac
    sql += "ORDER BY id DESC LIMIT %(limit)s"
    params["limit"] = limit
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def roaming_counts_24h() -> list[tuple]:
    """Handoffs per mac in the trailing 24h: [(mac, count), ...] count desc.
    Feeds the /ownership rapid-roamer strip (roaming is normal; RAPID
    roaming is the fault-monitoring signal — spine spec)."""
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mac, count(*) FROM mac_ownership_events "
                "WHERE at > now() - interval '24 hours' "
                "GROUP BY mac ORDER BY count(*) DESC, mac")
            return cur.fetchall()


def desired_summary() -> list[tuple]:
    """Return (owner_role, count, max_updated_at) rows from
    `desired_reservations`, one per owner role, for
    shadow_view.build_shadow(). May be empty when no owner has published
    desired rows yet."""
    sql = ("SELECT owner_role, count(*), max(updated_at) "
           "FROM desired_reservations GROUP BY 1 ORDER BY 1")
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def desired_by_role_kind() -> list[tuple]:
    """desired_reservations grouped: [(owner_role, kind, count, max_updated_at)]."""
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT owner_role, kind, count(*), max(updated_at) "
            "FROM desired_reservations GROUP BY owner_role, kind "
            "ORDER BY owner_role, kind").fetchall()


def work_record_counts() -> list[tuple]:
    """work_records pulse per role/kind: [(owner_role, kind, n_24h, n_7d)].
    Single pass over the 7-day window (the store is append-only; the
    (dut_id, at) index keeps this cheap at current volumes)."""
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT owner_role, kind, "
            " count(*) FILTER (WHERE at > now() - interval '24 hours'), "
            " count(*) "
            "FROM work_records WHERE at > now() - interval '7 days' "
            "GROUP BY owner_role, kind ORDER BY owner_role, kind").fetchall()


def observe_state_stats() -> tuple:
    """(row_count, oldest_poll_age_secs|None) over observe_state — the
    sensing-layer coverage tile. observe_state's timestamp column is
    last_polled (schema/versions/003_state_and_action_tables.py)."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*), "
            " extract(epoch FROM (now() - min(last_polled))) "
            "FROM observe_state").fetchone()
    return (row[0], float(row[1]) if row[1] is not None else None)


def active_lease_count() -> int:
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM kea.lease4 WHERE expire > now()").fetchone()[0]


def kea_hosts_count() -> int:
    with get_pool().connection() as conn:
        return conn.execute("SELECT count(*) FROM kea.hosts").fetchone()[0]


def reconcile_cadence() -> list[tuple]:
    """reconcile_actions over the trailing hour: [(action, outcome, count)].
    reconcile_actions' real columns are `ts` (not created_at) and `outcome`
    (not result) — see schema/versions/003_state_and_action_tables.py."""
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT action, outcome, count(*) FROM reconcile_actions "
            "WHERE ts > now() - interval '1 hour' "
            "GROUP BY action, outcome ORDER BY action, outcome").fetchall()


def intentional_flap_active() -> int:
    """Active intentional_flap rows. The table has no expires_at column;
    a hold is active while set_at + hold_seconds is still in the future
    (schema/versions/003_state_and_action_tables.py: switch, port,
    hold_seconds, reason, mac, set_at)."""
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM intentional_flap "
            "WHERE set_at + (hold_seconds * interval '1 second') > now()"
        ).fetchone()[0]


def roaming_role_counts_24h() -> list[tuple]:
    """Per-role handoff totals over 24h: [(role, inbound, outbound)]."""
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT role, sum(inbound)::int, sum(outbound)::int FROM ("
            "  SELECT to_role AS role, count(*) AS inbound, 0 AS outbound"
            "   FROM mac_ownership_events"
            "   WHERE at > now() - interval '24 hours' GROUP BY to_role"
            "  UNION ALL"
            "  SELECT from_role, 0, count(*) FROM mac_ownership_events"
            "   WHERE at > now() - interval '24 hours' AND from_role IS NOT NULL"
            "   GROUP BY from_role"
            ") t GROUP BY role ORDER BY role").fetchall()


def observe_state_all() -> dict[str, dict[str, Any]]:
    """Return {<switch>:<port>: {vars, last_polled, generation, resolved}}."""
    sql = """
        SELECT switch, port, vars, last_polled, generation, resolved
        FROM observe_state
        ORDER BY switch, port
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return {
        f"{switch}:{port}": {
            "switch": switch, "port": port,
            "vars": vars_, "last_polled": _iso(last_polled),
            "generation": generation, "resolved": resolved or {},
        }
        for switch, port, vars_, last_polled, generation, resolved in rows
    }


def observe_state_one(switch: str, port: str) -> dict[str, Any] | None:
    """Return a single observe_state row, or None if absent."""
    sql = """
        SELECT switch, port, vars, last_polled, generation, resolved
        FROM observe_state
        WHERE switch = %(switch)s AND port = %(port)s
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"switch": switch, "port": port})
            row = cur.fetchone()
    if row is None:
        return None
    return {"switch": row[0], "port": row[1], "vars": row[2],
            "last_polled": _iso(row[3]), "generation": row[4],
            "resolved": row[5] or {}}


def events_facets() -> dict[str, list]:
    """Distinct service/kind/switch values present in audit.events, for the
    /events filter dropdowns. Switches with no events never appear (so
    integration-rack switches are naturally excluded)."""
    out = {}
    with get_pool().connection() as conn:
        for col in ("service", "kind", "switch"):
            rows = conn.execute(
                f"SELECT DISTINCT {col} FROM audit.events "
                f"WHERE {col} IS NOT NULL AND {col} <> '' ORDER BY 1").fetchall()
            out[col + "s" if col != "switch" else "switches"] = [r[0] for r in rows]
    return out


def events(
    *,
    service: str | None = None,
    kind: str | None = None,
    mac: str | None = None,
    switch: str | None = None,
    port: str | None = None,
    since: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return audit.events rows matching the supplied filters.

    All filters are optional and combine via AND. `since` is an ISO timestamp.
    Default limit is 200; max is 5000.
    """
    where_clauses = []
    params: dict[str, Any] = {"limit": min(limit, 5000)}
    for col, val in [("service", service), ("kind", kind), ("mac", mac),
                      ("switch", switch), ("port", port)]:
        if val is not None:
            where_clauses.append(f"{col} = %({col})s")
            params[col] = val
    if since is not None:
        where_clauses.append("ts >= %(since)s")
        params["since"] = since

    where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    sql = (
        "SELECT id, ts, service, kind, mac, switch, port, payload "
        "FROM audit.events"
        f"{where_sql} "
        "ORDER BY ts DESC LIMIT %(limit)s"
    )
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [
        {"id": r[0], "ts": _iso(r[1]), "service": r[2], "kind": r[3],
         "mac": r[4], "switch": r[5], "port": r[6], "payload": r[7]}
        for r in rows
    ]


def events_for_services(services, limit=25):
    """Recent audit.events for a set of services (layer-scoped feed)."""
    if not services:
        return []
    sql = ("SELECT ts, service, kind, switch, port, mac, payload "
           "FROM audit.events WHERE service = ANY(%(s)s) "
           "ORDER BY ts DESC LIMIT %(l)s")
    with get_pool().connection() as conn:
        rows = conn.execute(sql, {"s": list(services), "l": int(limit)}).fetchall()
    return [{"ts": _iso(r[0]), "service": r[1], "kind": r[2], "switch": r[3],
             "port": r[4], "mac": r[5], "payload": r[6]} for r in rows]


def layer_anomalies(services, limit=20):
    """Recent non-clean signals scoped to a layer: deferred/failed/skipped
    acks + (when reconcile is in scope) failed/deferred reconcile_actions."""
    out = []
    with get_pool().connection() as conn:
        acks = conn.execute(
            "SELECT consumer, action, consumed_at, detail FROM consumer_acks "
            "WHERE consumer = ANY(%(s)s) AND action IN ('deferred','failed','skipped') "
            "ORDER BY consumed_at DESC LIMIT %(l)s",
            {"s": list(services), "l": int(limit)}).fetchall()
        for consumer, action, at, detail in acks:
            reason = (detail or {}).get("reason") if isinstance(detail, dict) else None
            out.append({"when": _iso(at), "source": consumer,
                        "what": f"{action}" + (f" → {reason}" if reason else "")})
        if "flax-reconcile" in services:
            ra = conn.execute(
                "SELECT ts, switch, port, action, outcome, reason FROM reconcile_actions "
                "WHERE outcome IN ('failed','deferred') ORDER BY ts DESC LIMIT %(l)s",
                {"l": int(limit)}).fetchall()
            for ts, sw, port, act, outcome, reason in ra:
                out.append({"when": _iso(ts), "source": "reconcile",
                            "what": f"{act} {sw}/{port} {outcome}" + (f" → {reason}" if reason else "")})
    out.sort(key=lambda d: (d["when"] or ""), reverse=True)
    return out[:limit]


def consumer_acks() -> list[dict[str, Any]]:
    """Return all rows of consumer_acks — one per (consumer, source)."""
    sql = """
        SELECT consumer, source, generation, action, consumed_at, detail
        FROM consumer_acks
        ORDER BY consumer, source
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [
        {"consumer": r[0], "source": r[1], "generation": r[2],
         "action": r[3], "consumed_at": _iso(r[4]), "detail": r[5]}
        for r in rows
    ]


def reconcile_actions_for_port(switch: str, port: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return recent reconcile_actions rows for (switch, port), newest first."""
    sql = ("SELECT ts, action, outcome, reason, detail FROM reconcile_actions "
           "WHERE switch=%(switch)s AND port=%(port)s ORDER BY ts DESC LIMIT %(limit)s")
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"switch": switch, "port": port, "limit": limit})
            return [{"ts": _iso(r[0]), "action": r[1], "outcome": r[2],
                     "reason": r[3], "detail": r[4]} for r in cur.fetchall()]


def reconcile_status_for_port(switch: str, port: str) -> dict[str, Any]:
    """LIVE lease-vs-reservation truth for the device(s) reserved at this port.

    reconcile_actions is HISTORY; the converged truth is whether each reserved
    mac on this port currently holds an active Kea lease whose IP equals its
    reservation IP. Source of truth: kea.hosts (reservation) LEFT JOIN
    kea.lease4 (current active lease, state=0).

    The port linkage lives in kea.hosts.user_context->'classify'->>'port',
    which stores the INTERNAL short form (et6b1). The caller passes the Arista
    canonical port (Ethernet6/1); we convert via triage_compat.internal_port.

    Convergence rule (kept deliberately simple):
      - a mac is converged iff lease is not None AND lease == reservation.
      - lease is None (no active lease) OR lease != reservation => live mismatch.

    Returns:
      {"converged": bool,          # all reserved macs converged (vacuously
                                   #   True when there are no reservations)
       "live_mismatches": int,     # count of reserved macs not converged
       "macs": [{"mac","reservation","lease"} ...]}  # empty => no reservation

    ONE round-trip. Returns the empty-but-converged shape on no rows.
    """
    from . import triage_compat as _tc
    port_internal = _tc.internal_port(port)
    sql = (
        "SELECT " + (_MAC_SQL % "h.dhcp_identifier") + " AS mac, "
        "host(('0.0.0.0'::inet) + h.ipv4_address) AS reservation, "
        "host(('0.0.0.0'::inet) + l.address) AS lease "
        "FROM kea.hosts h "
        "LEFT JOIN kea.lease4 l "
        "  ON l.hwaddr = h.dhcp_identifier AND l.state = 0 "
        "WHERE h.dhcp_identifier_type = 0 "
        "  AND (h.user_context::jsonb)->'classify'->>'switch' = %(switch)s "
        "  AND (h.user_context::jsonb)->'classify'->>'port'   = %(port_internal)s"
    )
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"switch": switch, "port_internal": port_internal})
            rows = cur.fetchall()
    macs = [{"mac": r[0], "reservation": r[1], "lease": r[2]} for r in rows]
    mismatches = sum(
        1 for m in macs
        if m["lease"] is None or m["lease"] != m["reservation"]
    )
    return {
        "converged": mismatches == 0,
        "live_mismatches": mismatches,
        "macs": macs,
    }


def device_one(mac: str) -> dict[str, Any] | None:
    """Return device row for the given mac, or None if not found."""
    sql = ("SELECT mac, switch, port, kind, latched FROM devices "
           "WHERE lower(mac)=lower(%(mac)s)")
    pool = get_pool()
    with pool.connection() as conn:
        r = conn.execute(sql, {"mac": mac}).fetchone()
    if not r:
        return None
    return {"mac": r[0], "switch": r[1], "port": r[2], "kind": r[3],
            "latched": r[4] or {}}


def reservations() -> list[dict]:
    """All Kea reservations, newest first by updated_at.

    Reads from the public.reservations view (Plan 5 migration 009) which
    flattens kea.hosts.user_context.classify + .operator_note into
    flat columns. Source-of-truth is kea.hosts (flax_classify writes
    the classify-managed fields; flax_control writes the operator_note
    column via the PATCH endpoint).
    """
    sql = """SELECT mac_hex, switch, port, kind, vid,
                    ipv4_address, hostname, subnet_id,
                    operator_note, generation, updated_at, ipv6_address,
                    aliases
               FROM reservations
              ORDER BY updated_at DESC NULLS LAST, mac_hex"""
    with get_pool().connection() as conn:
        cur = conn.execute(sql)
        rows = cur.fetchall()
    # The view exposes the MAC as `mac_hex` (text, no colons). Render the
    # canonical colon-lowercase `mac` here so the Reservations page and the
    # Leases page (which renders colon-form via _MAC_SQL) agree, and the
    # /devices/<mac> links match. mac_hex is kept so the operator-note form
    # action can keep posting the strict 12-hex form update_operator_note wants.
    return [{"mac": _colon_mac(m), "mac_hex": m,
             "switch": s, "port": p, "kind": k, "vid": v,
             "ipv4_address": ip, "hostname": h, "subnet_id": sid,
             "operator_note": note, "generation": g,
             "updated_at": ts, "ipv6_address": ip6, "aliases": al}
            for m, s, p, k, v, ip, h, sid, note, g, ts, ip6, al in rows]


# Renders a bytea column to canonical colon-lowercase MAC, e.g.
# "aa:bb:cc:00:00:01". Mirrors flax_reconcile.db._MAC_SQL. %s is filled by
# Python string formatting (the column name), NOT a psycopg parameter.
# The E'\\1:\\2:...' is the PostgreSQL escape-string literal for backreferences.
# Regular string (not f-string): py3.11-safe, no backslashes inside f-string braces.
_MAC_SQL = ("regexp_replace(encode(%s, 'hex'), "
            "'(..)(..)(..)(..)(..)(..)', E'\\\\1:\\\\2:\\\\3:\\\\4:\\\\5:\\\\6')")


def leases() -> list[dict[str, Any]]:
    """Active Kea leases (v4 + v6), sorted by IP.

    Mirrors flax_reconcile.db.read_active_leases / flax_observe.host_probe:
      - kea.lease4: address is host-order int4; render via host('0.0.0.0'::inet + addr).
      - hwaddr is bytea; render canonical colon-lowercase MAC via _MAC_SQL.
      - state=0 is "active" (Kea lease states: 0=default/active, 1=declined, 2=expired-reclaimed).
      - expire is already `timestamp with time zone` in Kea's Postgres backend
        (NOT a unix epoch) -- select it directly; to_timestamp() errors on a tz value.
    kea.lease6 is unioned in when present (address is already inet/text there).
    Returns [{mac, ip, subnet_id, expires, state}] sorted by ip.
    """
    sql_v4 = (
        "SELECT " + (_MAC_SQL % "hwaddr") + " AS mac, "
        "host(('0.0.0.0'::inet) + address) AS ip, "
        "subnet_id, expire AS expires, state "
        "FROM kea.lease4 WHERE state = 0"
    )
    # v6 leases keyed by DUID have a NULL hwaddr -> a blank MAC cell. Surface
    # the DUID (hex) as a 7th column so the row isn't blank; the dict builder
    # below falls back to "duid:<hex>" when the rendered MAC is NULL.
    sql_v6 = (
        "SELECT " + (_MAC_SQL % "hwaddr") + " AS mac, "
        "host(address::inet) AS ip, "
        "subnet_id, expire AS expires, state, encode(duid, 'hex') AS duid "
        "FROM kea.lease6 WHERE state = 0"
    )
    pool = get_pool()
    rows: list[tuple] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_v4)
            # v4 rows have no duid column; pad to the v6 shape.
            rows.extend(r + (None,) for r in cur.fetchall())
            # lease6 is optional — a site without v6 leases (or without the
            # table) shouldn't break the page. Swallow the lookup error.
            try:
                cur.execute(sql_v6)
                rows.extend(cur.fetchall())
            except Exception:
                pass
    out = []
    for r in rows:
        mac, duid = r[0], r[5]
        if not mac and duid:
            # DUID-keyed v6 lease: no hwaddr. Show the DUID so the cell isn't
            # blank (and the row isn't mistaken for an unidentified lease).
            mac = "duid:" + duid
        out.append({"mac": mac, "ip": r[1], "subnet_id": r[2],
                    "expires": _iso(r[3]), "state": r[4], "duid": duid})
    out.sort(key=lambda d: _ip_sort_key(d["ip"]))
    return out


def reconcile_request_for_mac(mac: str) -> dict[str, Any] | None:
    """Return the most recent reconcile_requests row for this MAC, or None.

    One row is enough — it covers both the open-action case and the
    just-finished case. Columns: reason, status, attempts, outcome,
    ts (iso), completed_at (iso).
    """
    sql = (
        "SELECT reason, status, attempts, outcome, ts, completed_at "
        "FROM reconcile_requests "
        "WHERE lower(mac) = lower(%(mac)s) "
        "ORDER BY ts DESC LIMIT 1"
    )
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"mac": mac})
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "reason": row[0],
        "status": row[1],
        "attempts": row[2],
        "outcome": row[3],
        "ts": _iso(row[4]),
        "completed_at": _iso(row[5]),
    }


def _ip_sort_key(ip: Any) -> tuple:
    """Sort key that orders dotted-quad IPs numerically; falls back to string."""
    if isinstance(ip, str) and ip.count(".") == 3:
        try:
            return (0, tuple(int(o) for o in ip.split(".")))
        except ValueError:
            pass
    return (1, (str(ip),))


def _mac_hex(mac: str) -> str:
    """Strip separators and lowercase a MAC for use with decode(..., 'hex').

    Accepts colon-, dash- or dot-separated (or already bare) input.
    E.g. 'AA:BB:CC:00:11:22' -> 'aabbcc001122', '1C-34-DA-7F-9D-32' -> '1c34da7f9d32'.
    """
    return mac.replace(":", "").replace("-", "").replace(".", "").lower()


def _colon_mac(mac: str) -> str:
    """Render any MAC form (bare hex / colon / dash / dot, any case) as
    canonical colon-lowercase, e.g. '1c34da7f9d32' -> '1c:34:da:7f:9d:32'.

    A 12-hex-digit string gets colons inserted; anything else is normalised
    by stripping separators then re-grouping (falls back to lowercasing the
    input verbatim if it isn't 12 hex digits)."""
    h = _mac_hex(mac)
    if len(h) == 12 and all(c in "0123456789abcdef" for c in h):
        return ":".join(h[i:i + 2] for i in range(0, 12, 2))
    return mac.lower()


def devices() -> list[dict[str, Any]]:
    """All enrolled devices, ordered by switch + port.

    Returns the columns that devices.html iterates:
      mac, switch, port, kind, family (from latched->>'family'), last_seen.
    """
    sql = """
        SELECT mac, switch, port, kind,
               latched->>'family' AS family,
               last_seen
        FROM devices
        ORDER BY switch, port
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [
        {"mac": r[0], "switch": r[1], "port": r[2], "kind": r[3],
         "family": r[4], "last_seen": _iso(r[5])}
        for r in rows
    ]


def device_full(mac: str) -> dict[str, Any] | None:
    """Cross-service device view joining all five services for one MAC.

    Port-form notes:
      - devices.port is INTERNAL form (et6b1).
      - switch_facts.ports keys + desired_port.port are Arista-canonical
        (Ethernet6/1). The caller must convert: triage_compat.arista_port(port).
      - observe_state.port is INTERNAL (et6b1); join on devices.port directly.
      - kea.hosts: MAC lives in dhcp_identifier (BYTEA, dhcp_identifier_type=0).

    Returns None if no device row for the given mac.
    The function issues a small number of targeted queries rather than one
    giant CTE, which keeps each query simple and mock-testable.
    """
    from . import triage_compat as _tc

    # 1. Core device row.
    dev_sql = (
        "SELECT mac, switch, port, kind, latched, last_seen "
        "FROM devices WHERE lower(mac)=lower(%(mac)s)"
    )
    pool = get_pool()
    with pool.connection() as conn:
        row = conn.execute(dev_sql, {"mac": mac}).fetchone()
    if row is None:
        return None

    mac_val, switch, port_int, kind, latched, last_seen = row
    # port_int is INTERNAL (et6b1); convert for switch_facts/desired_port lookups.
    port_arista = _tc.arista_port(port_int)

    result: dict[str, Any] = {
        "mac": mac_val,
        "switch": switch,
        "port": port_int,
        "kind": kind,
        "latched": latched or {},
        "last_seen": _iso(last_seen),
    }

    # 2. Kea reservation — dhcp_identifier is BYTEA; mac_hex strips colons.
    res_sql = (
        "SELECT host(('0.0.0.0'::inet) + ipv4_address) AS ip, hostname, "
        "       dhcp4_subnet_id, user_context "
        "FROM kea.hosts "
        "WHERE dhcp_identifier = decode(%(hex)s, 'hex') "
        "  AND dhcp_identifier_type = 0"
    )
    with pool.connection() as conn:
        res_row = conn.execute(res_sql, {"hex": _mac_hex(mac_val)}).fetchone()
    if res_row:
        import json as _json
        _ctx_raw = res_row[3]
        _ctx = _json.loads(_ctx_raw) if isinstance(_ctx_raw, str) and _ctx_raw else (
            _ctx_raw if isinstance(_ctx_raw, dict) else {}
        )
        result["reservation"] = {
            "ipv4_address": res_row[0],
            "hostname": res_row[1],
            "subnet_id": res_row[2],
            "classify": _ctx.get("classify", {}),
            "operator_note": _ctx.get("operator_note"),
        }
    else:
        result["reservation"] = None

    # 3. observe_state — port is INTERNAL (et6b1), join on port_int directly.
    obs_sql = (
        "SELECT vars, last_polled, generation, resolved "
        "FROM observe_state "
        "WHERE switch=%(switch)s AND port=%(port)s"
    )
    with pool.connection() as conn:
        obs_row = conn.execute(obs_sql, {"switch": switch, "port": port_int}).fetchone()
    if obs_row:
        result["observe"] = {
            "vars": obs_row[0] or {},
            "last_polled": _iso(obs_row[1]),
            "generation": obs_row[2],
            "resolved": obs_row[3] or {},
        }
    else:
        result["observe"] = None

    # 4. switch_facts port entry — keyed by Arista form (Ethernet6/1).
    sf_sql = (
        "SELECT ports->%(port)s "
        "FROM switch_facts "
        "WHERE switch=%(switch)s"
    )
    with pool.connection() as conn:
        sf_row = conn.execute(sf_sql, {"switch": switch, "port": port_arista}).fetchone()
    if sf_row and sf_row[0] is not None:
        result["port_facts"] = sf_row[0]
    else:
        result["port_facts"] = None

    # 5. desired_port — port is Arista-canonical.
    dp_sql = (
        "SELECT desired_vid, occupants, generation "
        "FROM desired_port "
        "WHERE switch=%(switch)s AND port=%(port)s"
    )
    with pool.connection() as conn:
        dp_row = conn.execute(dp_sql, {"switch": switch, "port": port_arista}).fetchone()
    if dp_row:
        result["desired"] = {
            "desired_vid": dp_row[0],
            "occupants": dp_row[1] or [],
            "generation": dp_row[2],
        }
    else:
        result["desired"] = None

    # 6. Last reconcile action for this device's switch+port (Arista form).
    ra_sql = (
        "SELECT ts, action, outcome, reason, detail "
        "FROM reconcile_actions "
        "WHERE switch=%(switch)s AND port=%(port)s "
        "ORDER BY ts DESC LIMIT 1"
    )
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(ra_sql, {"switch": switch, "port": port_arista})
            ra_row = cur.fetchone()
    if ra_row:
        result["last_reconcile_action"] = {
            "ts": _iso(ra_row[0]), "action": ra_row[1],
            "outcome": ra_row[2], "reason": ra_row[3], "detail": ra_row[4],
        }
    else:
        result["last_reconcile_action"] = None

    # 7. Recent audit events for this MAC (newest first, capped at 50).
    ev_sql = (
        "SELECT id, ts, service, kind, switch, port, payload "
        "FROM audit.events "
        "WHERE mac = %(mac)s "
        "ORDER BY ts DESC LIMIT 50"
    )
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(ev_sql, {"mac": mac_val})
            ev_rows = cur.fetchall()
    result["recent_events"] = [
        {"id": r[0], "ts": _iso(r[1]), "service": r[2], "kind": r[3],
         "switch": r[4], "port": r[5], "payload": r[6]}
        for r in ev_rows
    ]

    return result


def device_mac_by_switch_port(switch: str, port: str) -> str | None:
    """Resolve a (switch, internal-port) to a device mac. Prefers the bmc row,
    else the first device by kind. Accepts internal port form (et6b1)."""
    sql = ("SELECT mac FROM devices WHERE switch=%(s)s AND port=%(p)s "
           "ORDER BY (kind<>'bmc'), kind LIMIT 1")
    with get_pool().connection() as conn:
        row = conn.execute(sql, {"s": switch, "p": port}).fetchone()
    return row[0] if row else None


def device_macs_by_serial(serial: str) -> list[str]:
    """Device macs whose latched serial contains `serial` (case-insensitive)."""
    sql = ("SELECT mac FROM devices WHERE latched->>'serial' ILIKE %(q)s "
           "ORDER BY mac")
    with get_pool().connection() as conn:
        rows = conn.execute(sql, {"q": f"%{serial}%"}).fetchall()
    return [r[0] for r in rows]


def hostname_macs(text: str) -> list[str]:
    """Reservation macs whose hostname contains `text` (from public.reservations).
    Returns canonical colon-lowercase macs to match /devices/<mac> links."""
    sql = ("SELECT mac_hex FROM reservations WHERE hostname ILIKE %(q)s "
           "ORDER BY hostname LIMIT 25")
    with get_pool().connection() as conn:
        rows = conn.execute(sql, {"q": f"%{text}%"}).fetchall()
    return [_colon_mac(r[0]) for r in rows]
