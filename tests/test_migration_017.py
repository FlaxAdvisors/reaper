"""DB-backed tests for migration 017 (reconcile_flap_state circuit-breaker table).

Mirrors the repo skip convention used in test_migration_015.py:
try to open a connection from FLAX_TEST_DSN or the PG* env vars and
pytest.skip if no Postgres is reachable.  A single FLAX_TEST_DSN overrides
the PG* assembly when set.

These tests assert the *post-017* schema state, so they require `alembic
upgrade head` (through 017) to have run against the target DB first.
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
        c.execute("DELETE FROM reconcile_flap_state WHERE mac LIKE 'test_017:%'")
        yield c
    finally:
        try:
            c.execute("DELETE FROM reconcile_flap_state WHERE mac LIKE 'test_017:%'")
        except Exception:
            pass
        c.close()


def test_reconcile_flap_state_table_exists(conn):
    """reconcile_flap_state table is present after migration 017."""
    row = conn.execute(
        "SELECT to_regclass('public.reconcile_flap_state')"
    ).fetchone()
    assert row[0] == "reconcile_flap_state"


def test_reconcile_flap_state_columns(conn):
    """reconcile_flap_state has all expected columns."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'reconcile_flap_state'"
    ).fetchall()
    cols = {r[0] for r in rows}
    assert {"mac", "last_flap_at", "flaps_in_window", "window_start",
            "backoff_until", "faulted", "updated_at"} <= cols


def test_reconcile_flap_state_defaults(conn):
    """flaps_in_window defaults to 0, faulted defaults to FALSE, updated_at populated."""
    conn.execute(
        "INSERT INTO reconcile_flap_state (mac) VALUES ('test_017:aa:bb:cc:00:01')"
    )
    row = conn.execute(
        "SELECT flaps_in_window, faulted, updated_at "
        "FROM reconcile_flap_state WHERE mac = 'test_017:aa:bb:cc:00:01'"
    ).fetchone()
    assert row is not None
    assert row[0] == 0, "flaps_in_window should default to 0"
    assert row[1] is False, "faulted should default to FALSE"
    assert row[2] is not None, "updated_at should be populated by default"


def test_flax_reconcile_has_full_dml_on_flap_state():
    """flax_reconcile role has SELECT, INSERT, UPDATE, DELETE on reconcile_flap_state."""
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            row = c.execute(
                "SELECT has_table_privilege('flax_reconcile', 'reconcile_flap_state', %s)",
                (privilege,),
            ).fetchone()
            assert row is not None and row[0] is True, (
                f"flax_reconcile missing {privilege} on reconcile_flap_state"
            )
    finally:
        c.close()
