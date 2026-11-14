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
    assert "'barivia-leo'" in up and "'rrord-1033'" in up
    assert "'ovh-bare-tp'" in up and "'rrord-100'" in up
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
    orders = ("RRORD-1033", "RRORD-100", "ZZTEST-044")
    clean = lambda: c.execute("DELETE FROM post_order WHERE order_no = ANY(%s)", (list(orders),))
    clean()
    yield c
    clean(); c.close()


def test_044_applied_on_the_scratch_db(admin):
    cols = {r[0] for r in admin.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'post_order'")}
    assert "population" in cols
    admin.execute("INSERT INTO post_order (order_no) VALUES ('RRORD-1033'), ('RRORD-100')")
    admin.execute("INSERT INTO post_order (order_no, population) VALUES ('ZZTEST-044', 'x')")
    m = _load()
    for sql in m.BACKFILL:
        admin.execute(sql)
    got = dict(admin.execute(
        "SELECT order_no, population FROM post_order "
        "WHERE order_no IN ('RRORD-1033', 'RRORD-100', 'ZZTEST-044')").fetchall())
    assert got == {"RRORD-1033": "barivia-leo", "RRORD-100": "ovh-bare-tp", "ZZTEST-044": "x"}
    admin.execute("UPDATE post_order SET population = 'kept' WHERE order_no = 'RRORD-100'")
    for sql in m.BACKFILL:
        admin.execute(sql)
    assert admin.execute("SELECT population FROM post_order WHERE order_no = 'RRORD-100'"
                         ).fetchone()[0] == "kept"
