"""Tests for flax_classify.desired_port — upsert helper.

DB-backed: uses the same skip convention as test_flax_discover_integration.py
(try FLAX_TEST_DSN or libpq env vars, pytest.skip if no Postgres is reachable).
Requires the flax DB migrated to head (desired_port table from migration 002,
and flax_classify role having SELECT/INSERT/UPDATE/DELETE on it).

These tests SKIP locally (no DB); they run at deploy on the bang host.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")


# ── fixtures ────────────────────────────────────────────────────────────────

TEST_SWITCH = "rabbit-test-dp"
TEST_PORT = "Ethernet99/1"


def _dsn() -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    return " ".join([
        "host=" + os.environ.get("PGHOST", "127.0.0.1"),
        "port=" + os.environ.get("PGPORT", "5432"),
        "user=" + os.environ.get("PGUSER", "postgres"),
        "password=" + os.environ.get("PGPASSWORD", ""),
        "dbname=" + os.environ.get("PGDATABASE", "flax"),
    ])


def _cleanup(conn) -> None:
    conn.execute(
        "DELETE FROM desired_port WHERE switch = %s AND port = %s",
        (TEST_SWITCH, TEST_PORT),
    )


@pytest.fixture()
def dp_conn():
    """Scoped connection + cleanup; SKIP if no DB."""
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:
        pytest.skip("no DB available: " + str(exc))
    try:
        _cleanup(c)
        yield c
    finally:
        try:
            _cleanup(c)
        except Exception:
            pass
        c.close()


def _build_pool(dsn: str):
    from flax_classify.db import build_pool
    return build_pool(dsn)


# ── tests ───────────────────────────────────────────────────────────────────

def test_upsert_writes_and_bumps_generation(dp_conn):
    from flax_classify.desired_port import upsert_desired_port

    pool = _build_pool(_dsn())
    try:
        upsert_desired_port(pool, switch=TEST_SWITCH, port=TEST_PORT,
                            desired_vid=16, occupants={"bmc": "aa:bb:cc:00:00:01"})
        upsert_desired_port(pool, switch=TEST_SWITCH, port=TEST_PORT,
                            desired_vid=17, occupants={"bmc": "aa:bb:cc:00:00:01"})
        row = dp_conn.execute(
            "SELECT desired_vid, generation FROM desired_port "
            "WHERE switch = %s AND port = %s",
            (TEST_SWITCH, TEST_PORT),
        ).fetchone()
        assert row is not None, "expected a desired_port row but got none"
        assert row[0] == 17, "desired_vid should be updated to 17"
        assert row[1] == 2, "generation should be bumped to 2 on second write"
    finally:
        pool.close()
