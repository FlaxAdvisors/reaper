"""Tests for flax_observe.persistence.read_active_sentinels — Postgres-backed.

Convention mirrors test_flax_reconcile_sentinel.py:
  pytest.importorskip("psycopg") + DSN from FLAX_TEST_DSN / PG* env vars.
  No local Postgres -> all tests SKIP (expected in dev/CI without DB).
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from flax_reconcile import db as _reconcile_db  # noqa: E402  (need pool builder)

TEST_SWITCH = "rabbit-observe-sentinel-test"


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
        "DELETE FROM intentional_flap WHERE switch = %s",
        (TEST_SWITCH,))


@pytest.fixture()
def conn():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
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


def test_active_sentinel_within_window(conn):
    conn.execute(
        "INSERT INTO intentional_flap (switch, port, hold_seconds, reason, set_at) "
        "VALUES (%s, 'p', 2, 'r', NOW())",
        (TEST_SWITCH,))
    from flax_observe.persistence import read_active_sentinels
    pool = _reconcile_db.build_pool(_dsn())
    try:
        active = read_active_sentinels(pool, grace_secs=30)
    finally:
        pool.close()
    assert (TEST_SWITCH, "p") in active


def test_expired_sentinel_excluded(conn):
    conn.execute(
        "INSERT INTO intentional_flap (switch, port, hold_seconds, reason, set_at) "
        "VALUES (%s, 'old', 2, 'r', NOW() - INTERVAL '1 hour')",
        (TEST_SWITCH,))
    from flax_observe.persistence import read_active_sentinels
    pool = _reconcile_db.build_pool(_dsn())
    try:
        active = read_active_sentinels(pool, grace_secs=30)
    finally:
        pool.close()
    assert (TEST_SWITCH, "old") not in active
