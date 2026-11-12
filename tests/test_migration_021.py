"""DB-backed test for migration 021 (flax_reconcile SELECT on observe_state).

Mirrors test_migration_020's skip convention: connect via FLAX_TEST_DSN or PG*
env vars and pytest.skip when no Postgres is reachable. Asserts the post-021
schema state, so it requires `alembic upgrade head` (through 021) to have run
against the target DB first.
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


def test_flax_reconcile_has_select_on_observe_state(conn):
    """flax_reconcile is granted SELECT on observe_state (it had NONE) so
    read_installing_ports can gate the kick on install-state."""
    rows = conn.execute(
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = 'flax_reconcile' AND table_schema = 'public' "
        "AND table_name = 'observe_state'"
    ).fetchall()
    privs = {r[0] for r in rows}
    assert "SELECT" in privs, (
        f"flax_reconcile missing SELECT on observe_state, got {privs}")
