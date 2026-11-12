"""Migration 037: rackpost.* order archive tables, flax_control the only grantee.

Static tests load the migration from the cwd's code tree, falling back to the
imported package's tree (see test_flax_post_migration_034.py). The DB test reads
the INSTALLED grants, so it needs `alembic upgrade head` on the LOCAL test cluster
and refuses any database without `_retention_scratch_marker` -- never a bang.
"""
import importlib.util
import os
import pathlib
import re

import pytest

import flax_post

psycopg = pytest.importorskip("psycopg")

TABLES = ("order_device", "order_mac", "order_component", "order_rung")


def _load():
    rel = "schema/versions/037_rackpost_order_archive.py"
    p = pathlib.Path.cwd() / rel
    if not p.exists():
        p = pathlib.Path(flax_post.__file__).resolve().parent.parent / rel
    spec = importlib.util.spec_from_file_location("m037", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sql(m, monkeypatch, fn="upgrade"):
    seen = []
    monkeypatch.setattr(m.op, "execute", lambda sql: seen.append(sql))
    getattr(m, fn)()
    return "\n".join(seen).lower()


def test_037_static(monkeypatch):
    m = _load()
    assert (m.revision, m.down_revision) == ("037000000001", "036000000001")
    blob = _sql(m, monkeypatch)
    for t in TABLES:
        assert f"create table rackpost.{t}" in blob
        assert f"grant select, insert, delete on rackpost.{t} to flax_control" in blob
    assert set(re.findall(r"\bto (\w+)", blob)) == {"flax_control"}
    assert "update" not in blob
    assert "primary key (order_id, rack, pos)" in blob
    assert "drop schema rackpost cascade" in _sql(m, monkeypatch, "downgrade")


def _dsn() -> str:
    pw = os.environ.get("PGPASSWORD", "")      # "password= dbname=x" would eat dbname
    return " ".join([
        f"host={os.environ.get('PGHOST', '127.0.0.1')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={os.environ.get('PGUSER', 'postgres')}",
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
    ] + ([f"password={pw}"] if pw else []))


def test_037_installed_grants():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    with c:
        if c.execute("SELECT to_regclass('public._retention_scratch_marker')").fetchone()[0] is None:
            pytest.skip("not the local scratch cluster (no _retention_scratch_marker)")
        if c.execute("SELECT to_regnamespace('rackpost')").fetchone()[0] is None:
            pytest.skip("migration 037 not applied (alembic upgrade head)")
        rows = c.execute(
            "SELECT table_name, grantee, privilege_type FROM information_schema.role_table_grants"
            " WHERE table_schema = 'rackpost' AND grantee <> current_user").fetchall()
    # 040 adds order_artifact under the same single-writer grant
    assert set(rows) == {(t, "flax_control", p) for t in TABLES + ("order_artifact",)
                         for p in ("SELECT", "INSERT", "DELETE")}
