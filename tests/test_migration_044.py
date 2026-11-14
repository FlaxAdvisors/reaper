"""Migration 044: post_order.population (post lane; order labels live on the order)."""
import importlib.util
import os
import pathlib

import pytest

import flax_post

ROOT = pathlib.Path(flax_post.__file__).resolve().parent.parent


def _load():
    p = ROOT / "schema" / "versions" / "044_post_order_population.py"
    spec = importlib.util.spec_from_file_location("m044", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sql(m, monkeypatch, fn="upgrade"):
    seen = []
    monkeypatch.setattr(m.op, "execute", lambda sql: seen.append(sql))
    getattr(m, fn)()
    return " ".join(" ".join(seen).lower().split())


def test_044_static(monkeypatch):
    m = _load()
    assert (m.revision, m.down_revision) == ("044000000001", "043000000001")
    assert m.__doc__.startswith("post:")
    up = _sql(m, monkeypatch)
    assert "alter table post_order add column if not exists population text" in up
    assert "rrord" not in up              # derived from the data, not per-site literals
    assert up.count("population is null") == 2
    assert " to flax_" not in up  # no grant change
    assert "drop column if exists population" in _sql(m, monkeypatch, "downgrade")


def _dsn(user):
    e = os.environ.get
    return (f"host={e('PGHOST', '127.0.0.1')} port={e('PGPORT', '5432')} "
            f"dbname={e('PGDATABASE', 'flax')} user={user}")


@pytest.fixture()
def admin():
    psycopg = pytest.importorskip("psycopg")
    try:
        c = psycopg.connect(_dsn("postgres"), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no DB: {exc}")
    if c.execute("SELECT to_regclass('public._retention_scratch_marker')").fetchone()[0] is None:
        c.close(); pytest.skip("not the local scratch cluster")
    clean = lambda: c.execute("DELETE FROM post_order WHERE order_no LIKE 'ZZTEST-044%'")
    clean()
    yield c
    clean(); c.close()


def test_044_applied_on_the_scratch_db(admin):
    cols = {r[0] for r in admin.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'post_order'")}
    assert "population" in cols
    m = _load()
    before = admin.execute("SELECT order_no, sales, customer, population FROM post_settings "
                           "WHERE id=1").fetchone()
    try:
        admin.execute("INSERT INTO post_order (order_no, sales, customer) VALUES "
                      "('ZZTEST-044A', 'SA', 'CA'), ('ZZTEST-044B', NULL, NULL), "
                      "('ZZTEST-044C', NULL, NULL)")
        admin.execute("INSERT INTO post_order (order_no, population) VALUES ('ZZTEST-044K', 'x')")
        nodes = [("aa:de:ad:44:00:01", "ZZTEST-044B", "leo"), ("aa:de:ad:44:00:02", "ZZTEST-044B", "leo"),
                 ("aa:de:ad:44:00:03", "ZZTEST-044C", "leo"), ("aa:de:ad:44:00:04", "ZZTEST-044C", "tp"),
                 ("aa:de:ad:44:00:05", "ZZTEST-044K", "y")]
        for mac, o, p in nodes:
            admin.execute("INSERT INTO post_node (bmc_mac, assigned_order_no, assigned_population) "
                          "VALUES (%s, %s, %s)", (mac, o, p))
        admin.execute("UPDATE post_settings SET order_no='ZZTEST-044A', sales='stale', customer=NULL, "
                      "population='hdr-pop' WHERE id=1")
        for sql in m.BACKFILL:
            admin.execute(sql)
        got = dict(admin.execute("SELECT order_no, population FROM post_order "
                                 "WHERE order_no LIKE 'ZZTEST-044%'").fetchall())
        # header order <- header population; unanimous node override; mixed -> none; kept
        assert got == {"ZZTEST-044A": "hdr-pop", "ZZTEST-044B": "leo", "ZZTEST-044C": None,
                       "ZZTEST-044K": "x"}
        # post_settings now mirrors the header order's record
        assert admin.execute("SELECT sales, customer, population FROM post_settings WHERE id=1"
                             ).fetchone() == ("SA", "CA", "hdr-pop")
        for sql in m.BACKFILL:                 # re-runnable
            admin.execute(sql)
        assert admin.execute("SELECT population FROM post_order WHERE order_no='ZZTEST-044K'"
                             ).fetchone()[0] == "x"
    finally:
        admin.execute("DELETE FROM post_node WHERE bmc_mac LIKE 'aa:de:ad:44:%'")
        admin.execute("UPDATE post_settings SET order_no=%s, sales=%s, customer=%s, population=%s "
                      "WHERE id=1", before)
