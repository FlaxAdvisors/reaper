"""rackpost order archive: the rackpost.* tables (migrations 037, 038), written on Close.

rackpost (flax_rack, on the bang) never writes Postgres itself; Close sends one
payload to POST /api/v1/order-archive, which lands here. flax_control is the
tables' single writer (grants: SELECT, INSERT, DELETE, migration 037).

replace() validates the whole payload before any DB access (BadValue -> 400),
then in ONE transaction deletes every row of that (order_id, rack) from the four
tables and inserts the payload's rows: a re-close of the same rack replaces, never
duplicates, and a sibling rack (another `rack` letter) is never touched.
Verdicts and artifact paths only; no log text is stored (spec §8.2). A device's
`sales` (039) is the order's business sales order number (a label, never a key).
`identity` (038) is its rendered identity read: {title, serial, rows: [[label,
value]], raw: {name: artifact path}}, or null.
"""
import json
import re
from datetime import datetime

from .db import get_pool
from .order_reservations import BadMac, _mac_hex

ORDER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
RACK_RE = re.compile(r"[a-z]?")
SALES_RE = re.compile(r"[A-Za-z0-9._-]{0,64}")
MAC_ROLES = ("host", "bmc", "iom1", "iom2", "fabric_a", "fabric_b", "switch")
TABLES = ("order_device", "order_mac", "order_component", "order_rung")


class BadValue(ValueError):
    """A malformed payload (raised before any DB access)."""


def _str(v, what, *, empty=True, null=False) -> str | None:
    if v is None and null:
        return None
    if not isinstance(v, str) or (not empty and not v):
        raise BadValue(f"bad {what}: {v!r}")
    return v


def _ts(v, what, *, null=False) -> str | None:
    if v is None and null:
        return None
    try:
        t = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        raise BadValue(f"bad {what}: {v!r}") from None
    if t.tzinfo is None:
        raise BadValue(f"{what} needs a timezone: {v!r}")
    return v


def _int(v, what) -> int | None:
    if v is None or v == "":
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise BadValue(f"bad {what}: {v!r}")
    return v


def _identity(v, pos) -> str | None:
    """A device's identity block -> its JSON text (jsonb), None when absent."""
    if v is None:
        return None
    what = f"{pos} identity"
    if not isinstance(v, dict) or set(v) - {"title", "serial", "rows", "raw"}:
        raise BadValue(f"bad {what}")
    title = _str(v.get("title"), f"{what} title", null=True)
    serial = _str(v.get("serial"), f"{what} serial", null=True)
    rows = v.get("rows") or []
    if not isinstance(rows, list) or not all(
            isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) for x in r)
            for r in rows):
        raise BadValue(f"{what} rows must be [label, value] string pairs")
    raw = v.get("raw") or {}
    if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(p, str) for k, p in raw.items()):
        raise BadValue(f"{what} raw must map names to artifact paths")
    return json.dumps({"title": title, "serial": serial, "rows": rows, "raw": raw})


def _list(payload, key) -> list[dict]:
    v = payload.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
        raise BadValue(f"{key} must be a list of objects")
    return v


def _rows(payload: dict) -> tuple[dict, dict[str, list[tuple]]]:
    if not isinstance(payload, dict):
        raise BadValue("body must be an object")
    oid, rack = payload.get("order_id"), payload.get("rack", "")
    if not isinstance(oid, str) or not ORDER_RE.fullmatch(oid):
        raise BadValue(f"bad order_id: {oid!r}")
    if not isinstance(rack, str) or not RACK_RE.fullmatch(rack):
        raise BadValue(f"bad rack: {rack!r}")
    key = {"order_id": oid, "rack": rack}
    customer = _str(payload.get("customer"), "customer", empty=False)
    site = _str(payload.get("site"), "site", null=True)
    sales = _str(payload.get("sales"), "sales", null=True)
    if sales is not None and not SALES_RE.fullmatch(sales):
        raise BadValue(f"bad sales: {sales!r}")
    closed_at = _ts(payload.get("closed_at"), "closed_at")
    order_dir = _str(payload.get("order_dir"), "order_dir", null=True)

    devices, seen = [], set()
    for d in _list(payload, "devices"):
        pos = _str(d.get("pos"), "device pos", empty=False)
        if pos in seen:
            raise BadValue(f"device {pos} listed twice")
        seen.add(pos)
        devices.append((oid, rack, customer, site, pos, _int(d.get("ou"), f"{pos} ou"),
                        *(_str(d.get(c), f"{pos} {c}", null=True) for c in
                          ("kind", "part", "hostname", "chassis_serial", "final_verdict",
                           "ha_mode")),
                        closed_at, order_dir, _identity(d.get("identity"), pos), sales or None))
    macs = []
    for m in _list(payload, "macs"):
        pos = _str(m.get("pos"), "mac pos", empty=False)
        role = m.get("role")
        if role not in MAC_ROLES:
            raise BadValue(f"{pos}: bad mac role {role!r}")
        try:
            h = _mac_hex(m.get("mac"))
        except BadMac as e:
            raise BadValue(f"{pos} {role}: {e}") from None
        macs.append((oid, rack, pos, role, ":".join(h[i:i + 2] for i in range(0, 12, 2))))
    comps = []
    for c in _list(payload, "components"):
        pos = _str(c.get("pos"), "component pos", empty=False)
        comps.append((oid, rack, pos, _str(c.get("kind"), f"{pos} kind", empty=False),
                      *(_str(c.get(k), f"{pos} {k}", null=True)
                        for k in ("model", "serial", "slot"))))
    rungs = []
    for r in _list(payload, "rungs"):
        pos = _str(r.get("pos"), "rung pos", empty=False)
        arts = r.get("artifacts") or []
        if not isinstance(arts, list) or not all(isinstance(a, str) for a in arts):
            raise BadValue(f"{pos}: artifacts must be a list of paths")
        rungs.append((oid, rack, pos, _str(r.get("stage"), f"{pos} stage", empty=False),
                      _str(r.get("rung"), f"{pos} rung", empty=False),
                      _str(r.get("verdict"), f"{pos} verdict", null=True),
                      _ts(r.get("finished_at"), f"{pos} finished_at", null=True),
                      json.dumps(arts)))
    return key, {"order_device": devices, "order_mac": macs,
                 "order_component": comps, "order_rung": rungs}


_INSERT = {
    "order_device": "INSERT INTO rackpost.order_device (order_id, rack, customer, site, "
                    "pos, ou, kind, part, hostname, chassis_serial, final_verdict, ha_mode, "
                    "closed_at, order_dir, identity, sales) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
    "order_mac": "INSERT INTO rackpost.order_mac (order_id, rack, pos, role, mac) "
                 "VALUES (%s, %s, %s, %s, %s)",
    "order_component": "INSERT INTO rackpost.order_component (order_id, rack, pos, kind, "
                       "model, serial, slot) VALUES (%s, %s, %s, %s, %s, %s, %s)",
    "order_rung": "INSERT INTO rackpost.order_rung (order_id, rack, pos, stage, rung, "
                  "verdict, finished_at, artifacts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
}


def replace(payload: dict) -> dict:
    """Replace (order_id, rack)'s archive rows with the payload's, in one transaction.
    -> {"devices": n, "components": n, "macs": n, "rungs": n}. BadValue before any DB
    access."""
    key, rows = _rows(payload)
    with get_pool().connection() as conn, conn.transaction():
        for t in TABLES:
            conn.execute(f"DELETE FROM rackpost.{t} WHERE order_id = %(order_id)s "
                         "AND rack = %(rack)s", key)
        with conn.cursor() as cur:
            for t in TABLES:
                if rows[t]:
                    cur.executemany(_INSERT[t], rows[t])
    return {"devices": len(rows["order_device"]), "components": len(rows["order_component"]),
            "macs": len(rows["order_mac"]), "rungs": len(rows["order_rung"])}
