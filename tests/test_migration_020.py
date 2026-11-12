"""DB-backed tests for migration 020 (ipv6_reservations grants + view column).

Mirrors test_migration_019's skip convention: connect via FLAX_TEST_DSN or PG*
env vars and pytest.skip when no Postgres is reachable. Asserts the post-020
schema state, so it requires `alembic upgrade head` (through 020) to have run
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


def test_reservations_view_has_ipv6_address_column(conn):
    """public.reservations exposes the new ipv6_address column (and still
    carries the existing columns flax-control depends on)."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'reservations'"
    ).fetchall()
    cols = {r[0] for r in rows}
    assert "ipv6_address" in cols, f"ipv6_address missing from view, got {cols}"
    # existing columns must be preserved -- flax-control queries them.
    for existing in ("mac_hex", "ipv4_address", "hostname", "subnet_id",
                     "switch", "port", "kind", "vid", "operator_note",
                     "generation", "updated_at"):
        assert existing in cols, f"view lost existing column {existing}"


def test_flax_classify_has_insert_on_ipv6_reservations(conn):
    """flax_classify is granted DML on kea.ipv6_reservations (it had NONE)."""
    rows = conn.execute(
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = 'flax_classify' AND table_schema = 'kea' "
        "AND table_name = 'ipv6_reservations'"
    ).fetchall()
    privs = {r[0] for r in rows}
    for needed in ("INSERT", "SELECT", "UPDATE", "DELETE"):
        assert needed in privs, (
            f"flax_classify missing {needed} on kea.ipv6_reservations, "
            f"got {privs}")


def test_flax_classify_has_usage_on_reservation_id_seq(conn):
    """flax_classify can use the reservation_id sequence (the INSERT relies on
    its nextval() column default)."""
    row = conn.execute(
        "SELECT has_sequence_privilege("
        "'flax_classify', "
        "'kea.ipv6_reservations_reservation_id_seq', 'USAGE')"
    ).fetchone()
    assert row is not None and row[0] is True, (
        "flax_classify missing USAGE on "
        "kea.ipv6_reservations_reservation_id_seq")
