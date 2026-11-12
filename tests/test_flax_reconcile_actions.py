"""Tests for flax_reconcile.actions.log_action.

DB-convention: pytest.importorskip("psycopg") + DSN from FLAX_TEST_DSN/PG* env
vars + pytest.skip if no DB reachable.  No local Postgres -> all tests SKIP
(expected).  Mirrors test_flax_reconcile_sentinel.py pattern exactly.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from flax_reconcile import db  # noqa: E402
from flax_reconcile.actions import log_action  # noqa: E402

TEST_SWITCH = "rabbit-actions-test"
TEST_PORT = "Ethernet7/1"


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
        "DELETE FROM reconcile_actions WHERE switch = %s",
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


def test_log_action_inserts_row(conn):
    pool = db.build_pool(_dsn())
    try:
        log_action(pool, switch=TEST_SWITCH, port=TEST_PORT, action="switch_flap",
                   detail={"mac": "aa:bb:cc:00:00:01", "rung": "switch_flap"},
                   outcome="success", reason="mismatch")
        row = conn.execute(
            "SELECT action, outcome, detail->>'rung' FROM reconcile_actions "
            "WHERE switch = %s ORDER BY id DESC LIMIT 1",
            (TEST_SWITCH,)).fetchone()
    finally:
        pool.close()

    assert row == ("switch_flap", "success", "switch_flap")


def test_log_action_reason_nullable(conn):
    pool = db.build_pool(_dsn())
    try:
        log_action(pool, switch=TEST_SWITCH, port=TEST_PORT, action="noop",
                   detail={"info": "no reason needed"},
                   outcome="skipped")
        row = conn.execute(
            "SELECT reason FROM reconcile_actions "
            "WHERE switch = %s AND action = 'noop' ORDER BY id DESC LIMIT 1",
            (TEST_SWITCH,)).fetchone()
    finally:
        pool.close()

    assert row[0] is None
