"""DB-backed tests for migration 018 (reconcile_flap_pending self-heal marker).

Mirrors the skip convention in test_migration_017.py: connect via FLAX_TEST_DSN
or PG* env vars and pytest.skip when no Postgres is reachable. Asserts the
post-018 schema state, so it requires `alembic upgrade head` (through 018) to
have run against the target DB first.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")


def _dsn() -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    return " ".join([
        f"host={os.environ.get('PGHOST', '127.0.0.1')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={os.environ.get('PGUSER', 'postgres')}",
        f"password={os.environ.get('PGPASSWORD', '')}",
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
    ])


@pytest.fixture()
def conn():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        c.execute("DELETE FROM reconcile_flap_pending WHERE switch LIKE 'test_018:%'")
        yield c
    finally:
        try:
            c.execute("DELETE FROM reconcile_flap_pending WHERE switch LIKE 'test_018:%'")
        except Exception:
            pass
        c.close()


def test_reconcile_flap_pending_table_exists(conn):
    row = conn.execute(
        "SELECT to_regclass('public.reconcile_flap_pending')").fetchone()
    assert row[0] == "reconcile_flap_pending"


def test_reconcile_flap_pending_columns(conn):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'reconcile_flap_pending'"
    ).fetchall()
    cols = {r[0] for r in rows}
    assert {"switch", "port", "mac", "set_at"} <= cols


def test_reconcile_flap_pending_pk_switch_port(conn):
    """(switch, port) is the primary key: a second insert for the same pair
    conflicts (UPSERT in mark_flap_pending relies on this)."""
    conn.execute(
        "INSERT INTO reconcile_flap_pending (switch, port, mac) "
        "VALUES ('test_018:sw', 'Ethernet1', 'aa:bb:cc:00:00:01')")
    conn.execute(
        "INSERT INTO reconcile_flap_pending (switch, port, mac) "
        "VALUES ('test_018:sw', 'Ethernet1', 'aa:bb:cc:00:00:02') "
        "ON CONFLICT (switch, port) DO UPDATE SET mac = EXCLUDED.mac")
    row = conn.execute(
        "SELECT mac FROM reconcile_flap_pending "
        "WHERE switch = 'test_018:sw' AND port = 'Ethernet1'").fetchone()
    assert row[0] == "aa:bb:cc:00:00:02"


def test_flax_reconcile_has_full_dml_on_flap_pending():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            row = c.execute(
                "SELECT has_table_privilege('flax_reconcile', "
                "'reconcile_flap_pending', %s)", (privilege,)).fetchone()
            assert row is not None and row[0] is True, (
                f"flax_reconcile missing {privilege} on reconcile_flap_pending")
    finally:
        c.close()
