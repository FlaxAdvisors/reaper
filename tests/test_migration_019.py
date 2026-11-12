"""DB-backed tests for migration 019 (kea.flax_release_stale_lease fn).

Mirrors the skip convention in test_migration_018.py: connect via FLAX_TEST_DSN
or PG* env vars and pytest.skip when no Postgres is reachable. Asserts the
post-019 schema state, so it requires `alembic upgrade head` (through 019) to
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
        yield c
    finally:
        c.close()


def test_flax_release_stale_lease_fn_exists(conn):
    """The SECURITY DEFINER fn exists in the kea schema with a single bytea arg."""
    row = conn.execute(
        "SELECT prosecdef FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'kea' AND p.proname = 'flax_release_stale_lease' "
        "AND pg_get_function_arguments(p.oid) = 'p_mac bytea'").fetchone()
    assert row is not None, "kea.flax_release_stale_lease(bytea) not found"
    assert row[0] is True, "fn must be SECURITY DEFINER"


def test_flax_release_stale_lease_search_path_set(conn):
    """The fn pins search_path=kea, public so the func_lease4_adel trigger's
    unqualified isJsonSupported() resolves when it fires under the DELETE."""
    row = conn.execute(
        "SELECT proconfig FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'kea' AND p.proname = 'flax_release_stale_lease'"
    ).fetchone()
    assert row is not None and row[0] is not None
    assert any(c.startswith("search_path=") and "kea" in c for c in row[0]), (
        f"fn search_path must include kea, got {row[0]}")


def test_flax_reconcile_has_execute_on_fn(conn):
    """flax_reconcile is granted EXECUTE on the fn (and nothing more is needed
    -- it has only SELECT on kea.lease4)."""
    row = conn.execute(
        "SELECT has_function_privilege("
        "'flax_reconcile', 'kea.flax_release_stale_lease(bytea)', 'EXECUTE')"
    ).fetchone()
    assert row is not None and row[0] is True, (
        "flax_reconcile missing EXECUTE on kea.flax_release_stale_lease(bytea)")
