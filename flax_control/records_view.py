# flax_control/records_view.py
"""Phase-4 work-record retrieval (read-only; flax_control holds SELECT only).

Owns the "most recent assembly" semantics the spec pins on the API: a
mac/serial lookup resolves to the LATEST (p0_mac, serial) pairing by default;
prior assemblies ("this component's earlier lives") appear only under
assembly=all — no caller re-derives this. Recency comes from the migration-031
`dut_current` view (dut ⋈ latest-record-at), the same definition psql users
query directly.
"""
import re

from .db import get_pool

_MAX_LIMIT = 1000

_HEX12 = re.compile(r"^[0-9a-f]{12}$")


def _norm_mac(mac):
    """Lowercase colon-separated normal form, or None if not a MAC.

    Deliberately duplicated from flax_post/records.py::norm_mac — the
    writer's normal form a lookup mac must match. flax_control must not
    import flax_post (self-contained role-package rule; see the
    consumer_acks precedent for per-package duplication over shared
    libraries).
    """
    if not mac:
        return None
    m = str(mac).strip().lower().replace("-", ":").replace(".", "")
    if ":" not in m and _HEX12.match(m):
        m = ":".join(m[i:i + 2] for i in range(0, 12, 2))
    parts = m.split(":")
    if len(parts) != 6 or not all(len(p) == 2 for p in parts):
        return None
    return m


def _iso(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def _rows_to_duts(rows):
    return [{"dut_id": r[0], "p0_mac": r[1], "serial": r[2],
             "first_seen": _iso(r[3]), "last_record_at": _iso(r[4])}
            for r in rows]


def _apply_assembly(duts, assembly):
    """current (default) = most-recent assembly only; all = every pairing."""
    return duts if assembly == "all" else duts[:1]


def lookup_duts(mac=None, serial=None, assembly="current"):
    where, params = [], []
    if mac:
        normed = _norm_mac(mac)
        if normed is None:
            # An unnormalizable mac can never match any stored row; the
            # constraints are ANDed, so bail before touching the DB even if
            # a serial was also supplied.
            return []
        where.append("p0_mac = %s")
        params.append(normed)
    if serial:
        where.append("serial = %s")
        params.append(serial)
    if not where:
        return []
    sql = ("SELECT dut_id, p0_mac, serial, first_seen, last_record_at "
           "FROM dut_current WHERE " + " AND ".join(where) +
           " ORDER BY last_record_at DESC NULLS LAST, first_seen DESC")
    with get_pool().connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return _apply_assembly(_rows_to_duts(rows), assembly)


def dut_records(dut_id, *, kind=None, stage=None, since=None, limit=200):
    where, params = ["dut_id = %s"], [dut_id]
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if stage:
        where.append("stage = %s")
        params.append(stage)
    if since:
        where.append("at > %s")
        params.append(since)
    params.append(max(1, min(int(limit), _MAX_LIMIT)))
    sql = ("SELECT id, owner_role, kind, stage, keys, payload, at "
           "FROM work_records WHERE " + " AND ".join(where) +
           " ORDER BY at DESC, id DESC LIMIT %s")
    with get_pool().connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [{"id": r[0], "owner_role": r[1], "kind": r[2], "stage": r[3],
             "keys": r[4], "payload": r[5], "at": _iso(r[6])} for r in rows]


def records_by_key(key, value, *, limit=200):
    """The role-lens slice (post: order/customer, per its registry record_keys).
    `key` is interpolated from a fixed whitelist, never from user input."""
    if key not in ("order", "customer"):
        raise ValueError("unsupported record key: %r" % key)
    # The key is interpolated as a LITERAL expression (not a bind param) so
    # the query matches migration 031's expression indexes
    # ((keys->>'order'))/((keys->>'customer')) — Postgres cannot match a
    # parameterized `keys->>%s` against those indexes, which would otherwise
    # force a seq-scan of the ever-growing append-only work_records table.
    # `key` is whitelist-constant above, never user input; the VALUE stays a
    # bind param.
    key_expr = "w.keys->>'order'" if key == "order" else "w.keys->>'customer'"
    sql = ("SELECT w.id, w.dut_id, d.p0_mac, d.serial, w.owner_role, w.kind, "
           "w.stage, w.keys, w.payload, w.at "
           "FROM work_records w JOIN dut d USING (dut_id) "
           "WHERE " + key_expr + " = %s ORDER BY w.at DESC, w.id DESC LIMIT %s")
    with get_pool().connection() as conn:
        rows = conn.execute(
            sql, (value, max(1, min(int(limit), _MAX_LIMIT)))).fetchall()
    return [{"id": r[0], "dut_id": r[1], "p0_mac": r[2], "serial": r[3],
             "owner_role": r[4], "kind": r[5], "stage": r[6], "keys": r[7],
             "payload": r[8], "at": _iso(r[9])} for r in rows]


def detect_term(term):
    """Search-term detection for the /records smart box.
    Returns ("mac", normalized) | ("dut_id", int) | ("text", stripped).
    Order matters: mac shapes first (incl. bare-12-hex), then all-digits as
    dut_id, else free text (tried as serial, then order, then customer)."""
    s = str(term or "").strip()
    mac = _norm_mac(s)
    if mac is not None:
        return ("mac", mac)
    if s.isdigit():
        return ("dut_id", int(s))
    return ("text", s)


def dut_by_id(dut_id):
    """One dut_current row by id, shaped like lookup_duts items, or None."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT dut_id, p0_mac, serial, first_seen, last_record_at "
            "FROM dut_current WHERE dut_id = %s", (dut_id,)).fetchone()
    return _rows_to_duts([row])[0] if row else None


def duts_for_key(key, value, limit=50):
    """Distinct DUTs having work_records under keys->>key = value, with a
    record count each. Key is whitelist-constant (same rule as
    records_by_key); the literal expression keeps the migration-031
    expression indexes usable."""
    if key not in ("order", "customer"):
        raise ValueError("unsupported record key: %r" % key)
    key_expr = "w.keys->>'order'" if key == "order" else "w.keys->>'customer'"
    sql = ("SELECT d.dut_id, d.p0_mac, d.serial, count(*) AS records "
           "FROM work_records w JOIN dut d USING (dut_id) "
           "WHERE " + key_expr + " = %s "
           "GROUP BY d.dut_id, d.p0_mac, d.serial "
           "ORDER BY max(w.at) DESC LIMIT %s")
    with get_pool().connection() as conn:
        rows = conn.execute(sql, (value, max(1, min(int(limit), 200)))).fetchall()
    return [{"dut_id": r[0], "p0_mac": r[1], "serial": r[2], "records": r[3]}
            for r in rows]
