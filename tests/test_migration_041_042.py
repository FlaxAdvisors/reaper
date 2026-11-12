"""Migrations 041 (rackpost closed_by) + 042 (post per-node order assignment).
DB tests need `alembic upgrade head` on the LOCAL scratch cluster only."""
import importlib.util
import os
import pathlib

import pytest

import flax_post

psycopg = pytest.importorskip("psycopg")
MAC = "aa:de:ad:42:00:01"


def _load(name):
    rel = "schema/versions/" + name
    p = pathlib.Path.cwd() / rel
    if not p.exists():
        p = pathlib.Path(flax_post.__file__).resolve().parent.parent / rel
    spec = importlib.util.spec_from_file_location(name[:3], p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sql(m, monkeypatch, fn="upgrade"):
    seen = []
    monkeypatch.setattr(m.op, "execute", lambda sql: seen.append(sql))
    getattr(m, fn)()
    return " ".join(" ".join(seen).lower().split())


def test_041_static(monkeypatch):
    m = _load("041_rackpost_order_closed_by.py")
    assert (m.revision, m.down_revision) == ("041000000001", "040000000001")
    up = _sql(m, monkeypatch)
    assert "add column closed_by" in up and "check (closed_by in ('rackpost', 'post'))" in up
    assert "drop column closed_by" in _sql(m, monkeypatch, "downgrade")


def test_042_static(monkeypatch):
    m = _load("042_post_order_assignment.py")
    assert (m.revision, m.down_revision) == ("042000000001", "041000000001")
    assert m.__doc__.startswith("post:")
    up = _sql(m, monkeypatch)
    for s in ("alter table post_settings add column sales", "assigned_order_no", "assigned_sales",
              "assigned_customer", "assigned_population", "assigned_serial", "assigned_at",
              "create table post_run", "create table post_order", "create view post_node_assignment",
              "grant select on post_node_assignment to flax_classify"):
        assert s in up, s
    assert "on post_node to flax_classify" not in up
    down = _sql(m, monkeypatch, "downgrade")
    assert "drop view post_node_assignment" in down and "drop table post_run" in down


def _dsn(user):
    e = os.environ.get
    return (f"host={e('PGHOST', '127.0.0.1')} port={e('PGPORT', '5432')} "
            f"dbname={e('PGDATABASE', 'flax')} user={user}")


@pytest.fixture()
def admin():
    try:
        c = psycopg.connect(_dsn("postgres"), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no DB: {exc}")
    if c.execute("SELECT to_regclass('public._retention_scratch_marker')").fetchone()[0] is None:
        c.close(); pytest.skip("not the local scratch cluster")
    if c.execute("SELECT to_regclass('public.post_node_assignment')").fetchone()[0] is None:
        c.close(); pytest.skip("migration 042 not applied")
    clean = lambda: (c.execute("DELETE FROM post_run WHERE bmc_mac LIKE 'aa:de:ad:42:%'"),
                     c.execute("DELETE FROM post_node WHERE bmc_mac LIKE 'aa:de:ad:42:%'"))
    clean()
    yield c
    clean(); c.close()


def test_042_classify_reads_view_not_table(admin):
    c = psycopg.connect(_dsn("flax_classify"), connect_timeout=3, autocommit=True)
    try:
        c.execute("SELECT * FROM post_node_assignment").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT 1 FROM post_node")
    finally:
        c.close()


def test_view_hides_assignment_on_serial_mismatch(admin):
    admin.execute("INSERT INTO post_node (bmc_mac, serial, assigned_order_no, assigned_serial) "
                  "VALUES (%s, 'TESTSN-ORD-A', 'ZZTEST-1', 'TESTSN-ORD-A')", (MAC,))
    q = "SELECT bmc_mac::text, order_no FROM post_node_assignment WHERE bmc_mac = %s"
    assert admin.execute(q, (MAC,)).fetchall() == [(MAC, "ZZTEST-1")]
    admin.execute("UPDATE post_node SET serial='TESTSN-ORD-B' WHERE bmc_mac=%s", (MAC,))
    assert admin.execute(q, (MAC,)).fetchall() == []


def test_042_post_run_backfill_shape(admin):
    import json
    admin.execute("INSERT INTO post_node (bmc_mac, serial, vars) VALUES (%s, 'TESTSN-ORD-A', %s)",
                  (MAC, json.dumps({"result": {"run_id": "zz-run-1", "finished_at": 1762400000,
                                               "verdict": "pass", "order_no": "ZZTEST-1"}})))
    admin.execute(_load("042_post_order_assignment.py").BACKFILL_RUNS)
    row = admin.execute("SELECT finished_at, order_no, verdict FROM post_run "
                        "WHERE bmc_mac=%s AND run_id='zz-run-1'", (MAC,)).fetchone()
    import datetime
    assert row[0] == datetime.datetime(2025, 11, 6, 3, 33, 20, tzinfo=datetime.timezone.utc)
    assert row[1:] == ("ZZTEST-1", "pass")
