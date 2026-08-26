# flax_post/records.py
"""Append-only work-record writer — the phase-4 store's post-side producer arm.

Two core tables (migration 031): `dut` — the assembly registry, one insert-only
row per (p0_mac, serial) pairing; its UNIQUE constraint is the re-pairing
detector (known card + new chassis = new dut_id; the old assembly's records
are never touched) — and `work_records`, the append-only event log. flax_post
holds INSERT+SELECT only: append-only is grant-enforced, corrections are newer
records, never edits.

Event rules (spec 2026-07-05): append on EVENTS, never on polls — inventory
dedupes by content hash, sel appends deltas, fw-flash appends one record per
OUTCOME CHANGE (deduped against the device's latest fw-flash record, same
mechanism as inventory) — an enforce scan re-running the gauntlet over an
unchanged node appends nothing. Every caller wraps these in try/except so a
record failure never breaks the producing loop (lane isolation).

Kept flax_post-self-contained per the no-cross-import rule; a future triage
writer duplicates this deliberately (the consumer_acks precedent).
"""
import hashlib
import json
import re

from .db import get_pool

OWNER_ROLE = "post"

# Vendor placeholder "serials" — non-unique, so a pairing carrying one can't
# be trusted across a card swap. Recorded as an identity-fault (the
# reject-before-ship signal); the pairing itself still mints (unique per card).
_GARBAGE_SERIALS = (
    "to be defined", "to be filled", "not specified",
    "system serial number", "0123456789",
)

_HEX12 = re.compile(r"^[0-9a-f]{12}$")


def norm_mac(mac):
    """Lowercase colon-separated normal form, or None if not a MAC."""
    if not mac:
        return None
    m = str(mac).strip().lower().replace("-", ":").replace(".", "")
    if ":" not in m and _HEX12.match(m):
        m = ":".join(m[i:i + 2] for i in range(0, 12, 2))
    parts = m.split(":")
    if len(parts) != 6 or not all(len(p) == 2 for p in parts):
        return None
    return m


def serial_fault(serial):
    """Reason this serial is unusable as identity, or None if it looks real."""
    s = (serial or "").strip()
    if not s:
        return "serial missing/unreadable (FRU)"
    low = s.lower()
    for g in _GARBAGE_SERIALS:
        if g in low:
            return "placeholder serial: %r" % s
    return None


def canonical_hash(obj) -> str:
    """Stable content hash for change-dedupe (sorted-keys JSON, sha256 hex)."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def sel_delta(prev_entries, entries):
    """SEL entries not present in the previous snapshot, keyed (id, ts, event)."""
    seen = {(e.get("id"), e.get("ts"), e.get("event"))
            for e in (prev_entries or [])}
    return [e for e in (entries or [])
            if (e.get("id"), e.get("ts"), e.get("event")) not in seen]


def role_keys(settings) -> dict:
    """The post role's record_keys stamps ({order, customer}); nulls omitted.

    Key names follow /etc/flax/roles.d/post.json's record_keys declaration
    ("customer", "order") — the registry stays the sole declarer of role
    identity; this table has no role-named columns.
    """
    out = {}
    if settings.get("order_no"):
        out["order"] = settings["order_no"]
    if settings.get("customer"):
        out["customer"] = settings["customer"]
    return out


# ── DB layer (conn-level primitives + pool-level orchestrators) ─────────────

def resolve_dut(conn, p0_mac, serial) -> int:
    """Mint-or-get the (p0_mac, serial) assembly row; returns dut_id.

    The UNIQUE constraint is the re-pairing detector: a known card seen with
    a different serial inserts a NEW row (new dut_id) and the old assembly's
    records stay attached to the old one — invalidation by identity
    separation, never deletion. ON CONFLICT DO NOTHING + re-select handles
    the concurrent-mint race.
    """
    serial = (serial or "").strip()
    conn.execute(
        "INSERT INTO dut (p0_mac, serial) VALUES (%s, %s) "
        "ON CONFLICT (p0_mac, serial) DO NOTHING", (p0_mac, serial))
    row = conn.execute(
        "SELECT dut_id FROM dut WHERE p0_mac = %s AND serial = %s",
        (p0_mac, serial)).fetchone()
    return row[0]


def latest_payload(conn, dut_id, kind):
    """payload of the dut's newest record of `kind`, or None. The dedupe/delta
    baseline — stateless: the store itself is the memory."""
    row = conn.execute(
        "SELECT payload FROM work_records "
        "WHERE dut_id = %s AND kind = %s ORDER BY at DESC, id DESC LIMIT 1",
        (dut_id, kind)).fetchone()
    return row[0] if row else None


def append_record(conn, dut_id, *, kind, stage=None, keys=None, payload=None) -> int:
    row = conn.execute(
        "INSERT INTO work_records (dut_id, owner_role, kind, stage, keys, payload) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb) RETURNING id",
        (dut_id, OWNER_ROLE, kind, stage,
         json.dumps(keys or {}), json.dumps(payload or {}))).fetchone()
    return row[0]


def record_observation(*, p0_mac, serial, fru, sdr, sel, keys, pool=None) -> None:
    """Observe-cycle hook: inventory (hash-dedupe) + sel (delta) + identity-fault.

    Appends only on change; steady state writes nothing (the append-on-events
    rule). No p0_mac -> unresolved identity -> not a work record yet (skip).
    Caller wraps in try/except.
    """
    mac = norm_mac(p0_mac)
    if mac is None:
        return
    pool = pool or get_pool()
    with pool.connection() as conn, conn.transaction():
        dut_id = resolve_dut(conn, mac, serial)
        fault = serial_fault(serial)
        if fault:
            prev = latest_payload(conn, dut_id, "identity-fault")
            if not prev or prev.get("reason") != fault:
                append_record(conn, dut_id, kind="identity-fault", keys=keys,
                              payload={"reason": fault,
                                       "serial": (serial or "").strip()})
        # inventory = the STABLE hardware description: parsed FRU fields +
        # SDR sensor NAMES (presence topology — DIMM sensors appear/disappear
        # with DIMMs). Sensor VALUES are excluded: they change every poll.
        inv = {"fru": fru or {}, "sdr_sensors": sorted(sdr or {})}
        h = canonical_hash(inv)
        prev = latest_payload(conn, dut_id, "inventory")
        if not prev or prev.get("hash") != h:
            inv["hash"] = h
            append_record(conn, dut_id, kind="inventory", keys=keys, payload=inv)
        # sel: payload keeps the FULL snapshot ("entries") so the next delta
        # baselines against everything known, plus the new subset ("new").
        prev = latest_payload(conn, dut_id, "sel")
        new = sel_delta((prev or {}).get("entries"), sel)
        if new:
            append_record(conn, dut_id, kind="sel", keys=keys,
                          payload={"entries": sel or [], "new": new})


def node_identity(bmc_mac, pool=None):
    """(p0_mac, serial) for a post BMC via its durable post_node row, else
    (None, None). Queries by the raw bmc_mac string — post_node rows are
    written from the same queries.post_devices() mac strings the fwd agent
    holds, so no normalization mismatch is possible on the lookup key."""
    if not bmc_mac:
        return (None, None)
    pool = pool or get_pool()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT host_mac, serial FROM post_node WHERE bmc_mac = %s",
            (bmc_mac,)).fetchone()
    if not row:
        return (None, None)
    return (norm_mac(row[0]), row[1])


_FLASH_OUTCOME_KEYS = ("port", "terminal", "current", "target", "fault_reason")


def record_flash(*, p0_mac, serial, port, terminal, row, timeline, keys,
                 pool=None) -> None:
    """One fw-flash record per OUTCOME CHANGE (deduped against the device's
    latest fw-flash record, same mechanism as inventory) — an enforce scan
    re-running the gauntlet over an unchanged node appends nothing."""
    mac = norm_mac(p0_mac)
    if mac is None:
        return
    row = row or {}
    payload = {
        "port": port, "terminal": terminal,
        "current": row.get("current_version"),
        "target": row.get("target_version"),
        "fault_reason": row.get("fault_reason") or "",
        "percent": row.get("percent"),
        "timeline": timeline or [],
    }
    pool = pool or get_pool()
    with pool.connection() as conn, conn.transaction():
        dut_id = resolve_dut(conn, mac, serial)
        prev = latest_payload(conn, dut_id, "fw-flash")
        if prev is not None:
            prev_outcome = {k: prev.get(k) for k in _FLASH_OUTCOME_KEYS}
            new_outcome = {k: payload.get(k) for k in _FLASH_OUTCOME_KEYS}
            if prev_outcome == new_outcome:
                return
        append_record(conn, dut_id, kind="fw-flash", stage="Firmware",
                      keys=keys, payload=payload)


def record_action(*, p0_mac, serial, port, action, ok, detail, keys,
                  pool=None) -> None:
    """Enforce-mode owned action (set-pxe / power-on) — the 'agent touched
    this node' trail."""
    mac = norm_mac(p0_mac)
    if mac is None:
        return
    pool = pool or get_pool()
    with pool.connection() as conn, conn.transaction():
        dut_id = resolve_dut(conn, mac, serial)
        append_record(conn, dut_id, kind="fw-action", stage="Firmware",
                      keys=keys,
                      payload={"port": port, "action": action,
                               "ok": bool(ok), "detail": detail or ""})
