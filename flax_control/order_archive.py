"""rackpost order archive: the rackpost.* tables (migrations 037-040), written on Close.

rackpost (flax_rack, on the bang) never writes Postgres itself; Close sends one
payload to POST /api/v1/order-archive, which lands here. flax_control is the
tables' single writer (grants: SELECT, INSERT, DELETE, migration 037).

replace() validates the whole payload before any DB access (BadValue -> 400),
then in ONE transaction deletes every row of that (order_id, rack) from the four
tables and inserts the payload's rows: a re-close of the same rack replaces, never
duplicates, and a sibling rack (another `rack` letter) is never touched.
Since 040 the archived FILES are stored too (order_artifact: the gzip'd bytes of every
artifact the rungs reference plus the order's documents), so the archive needs
neither the order dir nor the bang that ran the order; each blob is decompressed and
checked against its size and sha256 before any DB access. A device's
`sales` (039) is the order's business sales order number (a label, never a key).
`closed_by` (041) names the tool that closed the order ('rackpost' default, or
'post' for the Post UI's Finish); a replace whose existing rows were closed by the
other tool is refused (CrossTool -> 409) before anything is deleted.
`identity` (038) is its rendered identity read: {title, serial, rows: [[label,
value]], raw: {name: artifact path}}, or null.
"""
import base64
import binascii
import gzip
import hashlib
import json
import re
import zlib
from datetime import datetime

from .db import get_pool
from .order_reservations import BadMac, _mac_hex

ORDER_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
RACK_RE = re.compile(r"[a-z]?")
SALES_RE = re.compile(r"[A-Za-z0-9._-]{0,64}")
MAC_ROLES = ("host", "bmc", "iom1", "iom2", "fabric_a", "fabric_b", "switch")
CLOSED_BY = ("rackpost", "post")
TABLES = ("order_device", "order_mac", "order_component", "order_rung", "order_artifact")
ARTIFACT_STATUS = ("stored", "too_large", "missing")
MAX_FILE = 5 * 1024 * 1024           # raw bytes, as rackpost's archive.MAX_FILE
# no quote, space, $ or backslash: the viewer passes the path through sh -c into psql
_PATH_RE = re.compile(r"[A-Za-z0-9._@+=:,-][A-Za-z0-9._@+=:,/-]{0,511}")


class CrossTool(Exception):
    """The (order_id, rack) archive was closed by the other tool; nothing written."""


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


def _artifact(a: dict) -> tuple:
    """One archived file -> (path, status, size, sha256, content_type, gzip bytes|None).
    A stored file's bytes must gunzip to its size and sha256."""
    path = a.get("path")
    if (not isinstance(path, str) or not _PATH_RE.fullmatch(path)
            or ".." in path.split("/") or "//" in path):
        raise BadValue(f"bad artifact path: {path!r}")
    status = a.get("status")
    if status not in ARTIFACT_STATUS:
        raise BadValue(f"{path}: bad artifact status {status!r}")
    size = _int(a.get("size"), f"{path} size")
    if status != "stored":
        return (path, status, size, None, None, None)
    sha = a.get("sha256")
    ctype = _str(a.get("content_type"), f"{path} content_type", empty=False)
    try:
        blob = base64.b64decode(a.get("gzip_b64") or "", validate=True)
        raw = gzip.decompress(blob)
    except (binascii.Error, OSError, EOFError, zlib.error, TypeError, ValueError):
        raise BadValue(f"{path}: content is not base64 gzip") from None
    if len(raw) > MAX_FILE:
        raise BadValue(f"{path}: {len(raw)} bytes is over the {MAX_FILE} cap")
    if size != len(raw) or sha != hashlib.sha256(raw).hexdigest():
        raise BadValue(f"{path}: content does not match its size / sha256")
    return (path, status, size, sha, ctype, blob)


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
    closed_by = payload.get("closed_by", "rackpost")
    if closed_by not in CLOSED_BY:
        raise BadValue(f"bad closed_by: {closed_by!r}")
    key["closed_by"] = closed_by
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
                        closed_at, order_dir, _identity(d.get("identity"), pos), sales or None,
                        closed_by))
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
    arts, seen_paths = [], set()
    for a in _list(payload, "artifacts"):
        row = _artifact(a)
        if row[0] in seen_paths:
            raise BadValue(f"artifact {row[0]} listed twice")
        seen_paths.add(row[0])
        arts.append((oid, rack, *row))
    return key, {"order_device": devices, "order_mac": macs,
                 "order_component": comps, "order_rung": rungs, "order_artifact": arts}


_INSERT = {
    "order_device": "INSERT INTO rackpost.order_device (order_id, rack, customer, site, "
                    "pos, ou, kind, part, hostname, chassis_serial, final_verdict, ha_mode, "
                    "closed_at, order_dir, identity, sales, closed_by) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
    "order_mac": "INSERT INTO rackpost.order_mac (order_id, rack, pos, role, mac) "
                 "VALUES (%s, %s, %s, %s, %s)",
    "order_component": "INSERT INTO rackpost.order_component (order_id, rack, pos, kind, "
                       "model, serial, slot) VALUES (%s, %s, %s, %s, %s, %s, %s)",
    "order_rung": "INSERT INTO rackpost.order_rung (order_id, rack, pos, stage, rung, "
                  "verdict, finished_at, artifacts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
    "order_artifact": "INSERT INTO rackpost.order_artifact (order_id, rack, path, status, "
                      "size, sha256, content_type, content) "
                      "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
}


def replace(payload: dict) -> dict:
    """Replace (order_id, rack)'s archive rows with the payload's, in one transaction.
    -> {"devices": n, "components": n, "macs": n, "rungs": n, "artifacts": n stored,
    "artifacts_not_stored": n}. BadValue before any DB
    access."""
    key, rows = _rows(payload)
    with get_pool().connection() as conn, conn.transaction():
        prior = {r[0] for r in conn.execute(
            "SELECT DISTINCT closed_by FROM rackpost.order_device "
            "WHERE order_id = %(order_id)s AND rack = %(rack)s", key).fetchall()}
        other = prior - {key["closed_by"]}
        if other:
            raise CrossTool(f"order {key['order_id']} rack {key['rack']!r} was closed by "
                            f"{sorted(other)[0]}; refusing a {key['closed_by']} replace")
        for t in TABLES:
            conn.execute(f"DELETE FROM rackpost.{t} WHERE order_id = %(order_id)s "
                         "AND rack = %(rack)s", key)
        with conn.cursor() as cur:
            for t in TABLES:
                if rows[t]:
                    cur.executemany(_INSERT[t], rows[t])
    return {"devices": len(rows["order_device"]), "components": len(rows["order_component"]),
            "macs": len(rows["order_mac"]), "rungs": len(rows["order_rung"]),
            "artifacts": sum(1 for r in rows["order_artifact"] if r[3] == "stored"),
            "artifacts_not_stored": sum(1 for r in rows["order_artifact"] if r[3] != "stored")}
