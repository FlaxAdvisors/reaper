"""DB-backed tests for migration 015 (reconcile_requests + kea triggers + grants).

Mirrors the repo skip convention used in test_migration_014_devices.py:
try to open a connection from FLAX_TEST_DSN or the PG* env vars and
pytest.skip if no Postgres is reachable. A single FLAX_TEST_DSN overrides
the PG* assembly when set.

These tests assert the *post-015* schema state, so they require `alembic
upgrade head` (through 015) to have run against the target DB first.
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
        # Best-effort cleanup of any stale test rows from a prior crash.
        c.execute("DELETE FROM reconcile_requests WHERE requested_by = 'test_migration_015'")
        yield c
    finally:
        try:
            c.execute("DELETE FROM reconcile_requests WHERE requested_by = 'test_migration_015'")
        except Exception:
            pass
        c.close()


def test_reconcile_requests_table_exists(conn):
    """reconcile_requests table is present after migration 015."""
    row = conn.execute(
        "SELECT to_regclass('public.reconcile_requests')"
    ).fetchone()
    assert row[0] == "reconcile_requests"


def test_reconcile_requests_columns(conn):
    """reconcile_requests has all expected columns including queue-specific ones."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'reconcile_requests'"
    ).fetchall()
    cols = {r[0] for r in rows}
    assert {"id", "ts", "requested_by", "mac", "switch", "port", "kind",
            "reason", "status", "attempts", "next_eligible",
            "claimed_at", "completed_at", "outcome"} <= cols


def test_reconcile_requests_status_default_and_check(conn):
    """status defaults to 'pending' and CHECK constraint enforces valid values."""
    conn.execute(
        "INSERT INTO reconcile_requests (requested_by, mac, reason) "
        "VALUES ('test_migration_015', 'aa:bb:cc:00:00:01', 'migration_test')"
    )
    row = conn.execute(
        "SELECT status, attempts FROM reconcile_requests "
        "WHERE requested_by = 'test_migration_015'"
    ).fetchone()
    assert row == ("pending", 0)

    with pytest.raises(Exception, match="reconcile_requests_status_check"):
        conn.execute(
            "INSERT INTO reconcile_requests (requested_by, mac, reason, status) "
            "VALUES ('test_migration_015', 'aa:bb:cc:00:00:02', 'test', 'invalid_status')"
        )


def test_notify_reconcile_request_trigger_fires_on_insert(conn):
    """INSERT on reconcile_requests fires a notify on channel 'reconcile_requests'."""
    listener = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    try:
        listener.execute("LISTEN reconcile_requests")
        conn.execute(
            "INSERT INTO reconcile_requests (requested_by, mac, reason) "
            "VALUES ('test_migration_015', 'aa:bb:cc:00:00:03', 'notify_test')"
        )
        notes = list(listener.notifies(timeout=2, stop_after=1))
        assert notes, "INSERT should fire a notify on channel 'reconcile_requests'"
        assert notes[0].channel == "reconcile_requests"
    finally:
        listener.close()


# ---------------------------------------------------------------------------
# Kea-half tests (trigger + grants).  These require a live Postgres with the
# kea schema present (braintree CI / manual run) — they skip in dev just like
# the reconcile_requests tests above.
# ---------------------------------------------------------------------------

def test_kea_hosts_change_notify_trigger_exists():
    """kea_hosts_change_notify trigger exists on kea.hosts after migration 015."""
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        row = c.execute(
            "SELECT trigger_name FROM information_schema.triggers "
            "WHERE event_object_schema = 'kea' "
            "  AND event_object_table = 'hosts' "
            "  AND trigger_name = 'kea_hosts_change_notify'"
        ).fetchone()
        assert row is not None, "trigger kea_hosts_change_notify not found on kea.hosts"
        assert row[0] == "kea_hosts_change_notify"
    finally:
        c.close()


def test_flax_reconcile_has_select_on_kea_tables():
    """flax_reconcile role has SELECT on kea.lease4, kea.lease6, kea.hosts."""
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        for table in ("kea.lease4", "kea.lease6", "kea.hosts"):
            row = c.execute(
                "SELECT has_table_privilege('flax_reconcile', %s, 'SELECT')",
                (table,),
            ).fetchone()
            assert row is not None and row[0] is True, (
                f"flax_reconcile missing SELECT on {table}"
            )
    finally:
        c.close()
