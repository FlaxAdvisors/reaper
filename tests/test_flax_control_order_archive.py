"""flax_control.order_archive: rackpost's Close archive (rackpost.* tables, migration 037).

replace() swaps one (order_id, rack)'s rows in one transaction: twice gives the same
rows (no duplicates), and a sibling rack's rows are never touched. Bad input is a 400
before any DB access.

DB tests connect AS flax_control (so they exercise 037's grants) to the LOCAL test
cluster only: they need `alembic upgrade head` there and refuse any database without
`_retention_scratch_marker` -- never a bang (memory feedback-no-db-access-from-the-vm).
"""
import base64
import contextlib
import gzip
import hashlib
import os

import pytest
pytest.importorskip("flax_control.order_archive")
from fastapi.testclient import TestClient  # noqa: E402

from flax_control import order_archive  # noqa: E402
from flax_control.app import app  # noqa: E402

psycopg = pytest.importorskip("psycopg")

ORDER = "ZZTEST-037"


def _payload(rack="", serial="SN-A1", n_dev=2):
    devs = [{"pos": f"P{i}", "ou": 10 + i, "kind": "compute", "part": "TiogaPass",
             "hostname": f"zz-p{i}", "chassis_serial": f"{serial}-{i}",
             "final_verdict": "deployed", "ha_mode": None} for i in range(n_dev)]
    return {
        "order_id": ORDER, "rack": rack, "customer": "Test Customer", "site": "eindhoven",
        "closed_at": "2026-10-30T10:00:00+00:00", "order_dir": "/tmp/zz",
        "devices": devs,
        "macs": [{"pos": "P0", "role": "host", "mac": "02:fe:37:00:00:01"},
                 {"pos": "P0", "role": "bmc", "mac": "02fe37000002"}],
        "components": [{"pos": "P0", "kind": "disk", "model": "Sandisk SSD PLUS",
                        "serial": "2228", "slot": "0000:01:00.0"}],
        "rungs": [{"pos": "P0", "stage": "done", "rung": "end-summary", "verdict": "pass",
                   "finished_at": "2026-10-30T09:59:00+00:00",
                   "artifacts": ["artifacts/P0/done9/end-summary/sel-elist.txt"]}],
    }


def _art(path, data=b"SEL line 1\n"):
    return {"path": path, "status": "stored", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "content_type": "text/plain",
            "gzip_b64": base64.b64encode(gzip.compress(data)).decode()}


# ---------------------------------------------------------------- route, no DB
def test_bad_order_id_400(monkeypatch):
    monkeypatch.setattr(order_archive, "get_pool", lambda: pytest.fail("DB touched"))
    c = TestClient(app)
    for bad in ({**_payload(), "order_id": "RR 101; drop"},
                {**_payload(), "rack": "B"},
                {**_payload(), "macs": [{"pos": "P0", "role": "host", "mac": "nope"}]},
                {**_payload(), "macs": [{"pos": "P0", "role": "ipmi", "mac": "02fe37000002"}]},
                {**_payload(), "closed_at": "yesterday"},
                {**_payload(), "devices": [{"pos": "P0", "identity": {"rows": [["a"]]}}]},
                {**_payload(), "devices": [{"pos": "P0", "identity": {"raw": ["x"]}}]},
                {**_payload(), "devices": [{"pos": "P0", "identity": {"text": "log"}}]},
                {**_payload(), "sales": "S ORD; drop"},
                {**_payload(), "sales": 101031},
                {**_payload(), "artifacts": [_art("../etc/passwd")]},
                {**_payload(), "artifacts": [_art("a b.txt")]},
                {**_payload(), "artifacts": [dict(_art("x.txt"), sha256="0" * 64)]},
                {**_payload(), "artifacts": [dict(_art("x.txt"), gzip_b64="not gzip!")]},
                {**_payload(), "artifacts": [dict(_art("x.txt"), status="lost")]},
                {**_payload(), "artifacts": [_art("x.txt"), _art("x.txt")]},
                {**_payload(), "closed_by": "triage"}):
        r = c.post("/api/v1/order-archive", json=bad)
        assert r.status_code == 400 and r.json()["error"]


# ---------------------------------------------------------------- DB (local only)
def _dsn(user) -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    pw = os.environ.get("PGPASSWORD", "")      # "password= dbname=x" would eat dbname
    return " ".join([
        f"host={os.environ.get('PGHOST', '127.0.0.1')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={user}",
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
    ] + ([f"password={pw}"] if pw else []))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def connection(self):
        return contextlib.nullcontext(self.conn)


def _clean(c):
    for t in order_archive.TABLES:
        c.execute(f"DELETE FROM rackpost.{t} WHERE order_id = %s", (ORDER,))


@pytest.fixture()
def conn(monkeypatch):
    try:
        c = psycopg.connect(_dsn("flax_control"), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    if c.execute("SELECT to_regclass('public._retention_scratch_marker')").fetchone()[0] is None:
        c.close()
        pytest.skip("not the local scratch cluster (no _retention_scratch_marker)")
    if c.execute("SELECT to_regclass('rackpost.order_device')").fetchone()[0] is None:
        c.close()
        pytest.skip("migration 037 not applied (alembic upgrade head)")
    _clean(c)
    monkeypatch.setattr(order_archive, "get_pool", lambda: _Pool(c))
    try:
        yield c
    finally:
        _clean(c)
        c.close()


def _rows(c, rack):
    return {t: sorted(map(tuple, c.execute(
        f"SELECT * FROM rackpost.{t} WHERE order_id = %s AND rack = %s ORDER BY 1",
        (ORDER, rack)).fetchall())) for t in order_archive.TABLES}


def test_replace_is_idempotent(conn):
    want = {"devices": 2, "components": 1, "macs": 2, "rungs": 1,
            "artifacts": 0, "artifacts_not_stored": 0}
    assert order_archive.replace(_payload()) == want
    first = _rows(conn, "")
    assert order_archive.replace(_payload()) == want
    assert _rows(conn, "") == first
    assert [len(first[t]) for t in order_archive.TABLES] == [2, 2, 1, 1, 0]
    macs = {str(r[4]) for r in first["order_mac"]}
    assert macs == {"02:fe:37:00:00:01", "02:fe:37:00:00:02"}
    # a re-close with fewer devices drops the old extra rows
    order_archive.replace(_payload(n_dev=1, serial="SN-B"))
    devs = _rows(conn, "")["order_device"]
    assert [(d[4], d[9]) for d in devs] == [("P0", "SN-B-0")]


def test_sibling_rack_untouched(conn):
    order_archive.replace(_payload("a", serial="SN-A"))
    order_archive.replace(_payload("b", serial="SN-B"))
    b = _rows(conn, "b")
    order_archive.replace(_payload("a", serial="SN-A2", n_dev=1))
    assert _rows(conn, "b") == b
    assert [d[9] for d in _rows(conn, "a")["order_device"]] == ["SN-A2-0"]


def test_failed_insert_keeps_old_rows(conn):
    order_archive.replace(_payload())
    before = _rows(conn, "")
    bad = _payload()
    bad["rungs"][0]["finished_at"] = "2026-13-45T00:00:00+00:00"    # parses? no -> 400
    with pytest.raises(order_archive.BadValue):
        order_archive.replace(bad)
    bad = _payload()
    bad["devices"][0]["ou"] = 2 ** 40                               # int out of range in PG
    with pytest.raises(psycopg.Error):
        order_archive.replace(bad)
    assert _rows(conn, "") == before                                # rolled back, not emptied


def test_identity_roundtrip(conn):
    """038: a device's identity block lands as jsonb; a device without one is NULL."""
    p = _payload()
    ident = {"title": "Board Serial", "serial": "J49110C3ZB1",
             "rows": [["Board Serial", "J49110C3ZB1"], ["IOM Location", "Side A"]],
             "raw": {"fruid-util-iom": "artifacts/JBOD01.iom1/d1/serial/fruid.txt"}}
    p["devices"][0]["identity"] = ident
    order_archive.replace(p)
    got = dict(conn.execute("SELECT pos, identity FROM rackpost.order_device "
                            "WHERE order_id = %s ORDER BY pos", (ORDER,)).fetchall())
    assert got == {"P0": ident, "P1": None}


def test_sales_roundtrip(conn):
    """039: the order's sales number lands on every device row; absent -> NULL."""
    order_archive.replace({**_payload(), "sales": "S-ORD101031"})
    got = {r[0] for r in conn.execute("SELECT sales FROM rackpost.order_device "
                                       "WHERE order_id = %s", (ORDER,)).fetchall()}
    assert got == {"S-ORD101031"}
    order_archive.replace(_payload())
    got = {r[0] for r in conn.execute("SELECT sales FROM rackpost.order_device "
                                       "WHERE order_id = %s", (ORDER,)).fetchall()}
    assert got == {None}


def test_artifacts_roundtrip_and_replace(conn):
    """040: stored files land gzip'd and come back byte-exact; path-only rows keep no
    content; a re-close replaces the set; an empty set (withdraw) leaves none."""
    data = b"Processor #0x1 | IERR\n" * 50
    arts = [_art("artifacts/P0/d1/done/sel-elist.txt", data),
            {"path": "artifacts/P0/d1/inventory/hwinfo.txt", "status": "too_large", "size": 9},
            {"path": "process-report.html", "status": "missing"}]
    out = order_archive.replace({**_payload(), "artifacts": arts})
    assert (out["artifacts"], out["artifacts_not_stored"]) == (1, 2)
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT path, status, size, content FROM rackpost.order_artifact WHERE order_id = %s",
        (ORDER,)).fetchall()}
    assert gzip.decompress(rows["artifacts/P0/d1/done/sel-elist.txt"][2]) == data
    assert rows["process-report.html"] == ("missing", None, None)
    order_archive.replace({**_payload(), "artifacts": arts[:1]})
    assert conn.execute("SELECT count(*) FROM rackpost.order_artifact WHERE order_id = %s",
                        (ORDER,)).fetchone()[0] == 1
    order_archive.replace({**_payload(), "artifacts": []})
    assert conn.execute("SELECT count(*) FROM rackpost.order_artifact WHERE order_id = %s",
                        (ORDER,)).fetchone()[0] == 0



# --- 041 closed_by + cross-tool guard (2026-11-06) ----------------------------
def _closed_by(c):
    return {r[0] for r in c.execute("SELECT DISTINCT closed_by FROM rackpost.order_device "
                                    "WHERE order_id = %s", (ORDER,)).fetchall()}


def test_closed_by_roundtrip_and_default(conn):
    order_archive.replace(_payload())
    assert _closed_by(conn) == {"rackpost"}
    _clean(conn)
    order_archive.replace({**_payload(), "closed_by": "post"})
    assert _closed_by(conn) == {"post"}


def test_cross_tool_replace_refused_rows_kept(conn):
    order_archive.replace({**_payload(), "closed_by": "post"})
    before = _rows(conn, "")
    with pytest.raises(order_archive.CrossTool):
        order_archive.replace(_payload())
    assert _rows(conn, "") == before
    order_archive.replace({**_payload(n_dev=1), "closed_by": "post"})      # same tool: fine
    assert len(_rows(conn, "")["order_device"]) == 1


def test_route_cross_tool_409(conn):
    order_archive.replace({**_payload(), "closed_by": "post"})
    r = TestClient(app).post("/api/v1/order-archive", json=_payload())
    assert r.status_code == 409 and "closed by post" in r.json()["error"]
