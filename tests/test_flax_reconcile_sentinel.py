"""Tests for flax_reconcile.sentinel.write_sentinel.

DB-convention: pytest.importorskip("psycopg") + DSN from FLAX_TEST_DSN/PG* env
vars + pytest.skip if no DB reachable.  No local Postgres -> all tests SKIP
(expected).  Mirrors test_flax_reconcile_db.py pattern exactly.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from flax_reconcile import db  # noqa: E402
from flax_reconcile.sentinel import write_sentinel  # noqa: E402

TEST_SWITCH = "rabbit-sentinel-test"
TEST_PORT = "Ethernet6/1"
TEST_MAC = "aa:bb:cc:00:00:01"


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
    conn.execute(
        "DELETE FROM intentional_flap WHERE switch = 's' AND port = 'p'")


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


def test_write_sentinel_upserts_row(conn):
    pool = db.build_pool(_dsn())
    try:
        write_sentinel(pool, switch=TEST_SWITCH, port=TEST_PORT,
                       hold_seconds=2, reason="enroll_reservation_changed",
                       mac=TEST_MAC)
        row = conn.execute(
            "SELECT hold_seconds, reason, mac FROM intentional_flap "
            "WHERE switch = %s AND port = %s",
            (TEST_SWITCH, TEST_PORT)).fetchone()
    finally:
        pool.close()

    assert row == (2, "enroll_reservation_changed", TEST_MAC)


def test_write_sentinel_is_idempotent_on_pk(conn):
    pool = db.build_pool(_dsn())
    try:
        for _ in range(2):
            write_sentinel(pool, switch="s", port="p", hold_seconds=2,
                           reason="r", mac=None)
        n = conn.execute(
            "SELECT count(*) FROM intentional_flap "
            "WHERE switch = 's' AND port = 'p'").fetchone()[0]
    finally:
        pool.close()

    assert n == 1
