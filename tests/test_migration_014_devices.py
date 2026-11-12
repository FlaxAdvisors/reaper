"""DB-backed tests for migration 014 (trim devices, guarded NOTIFY trigger).

Mirrors the repo skip convention (e.g. test_flax_observe_persistence.py's
test_resolved_round_trips_product_name): try to open a connection from the
libpq PG* env vars and pytest.skip if no Postgres is reachable. A single
FLAX_TEST_DSN overrides the PG* assembly when set.

These tests assert the *post-014* schema state, so they require `alembic
upgrade head` (through 014) to have run against the target DB first.
"""
import os
import time

import pytest

psycopg = pytest.importorskip("psycopg")

TEST_MAC = "aa:aa:aa:aa:aa:aa"


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
        # Best-effort cleanup of a stale throwaway row from a prior crash.
        c.execute("DELETE FROM devices WHERE mac = %s", (TEST_MAC,))
        yield c
    finally:
        try:
            c.execute("DELETE FROM devices WHERE mac = %s", (TEST_MAC,))
        except Exception:
            pass
        c.close()


def test_polled_and_classification_source_dropped(conn):
    """polled + classification_source are gone; the identity columns remain."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'devices'"
    ).fetchall()
    cols = {r[0] for r in rows}

    assert "polled" not in cols
    assert "classification_source" not in cols
    for keep in ("mac", "switch", "port", "kind", "latched", "generation"):
        assert keep in cols, f"expected column {keep!r} still present"


def _insert_test_device(conn):
    conn.execute(
        "INSERT INTO devices (mac, switch, port, kind, latched) "
        "VALUES (%s, %s, %s, %s, %s::jsonb)",
        (TEST_MAC, "rabbit-test", "swp1", "bmc", "{}"),
    )


def _drain(listen_conn, timeout=2.0):
    """Collect any notifies arriving within `timeout` seconds."""
    got = []
    for n in listen_conn.notifies(timeout=timeout, stop_after=1):
        got.append(n)
    return got


def test_insert_fires_notify(conn):
    listener = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    try:
        listener.execute("LISTEN devices")
        _insert_test_device(conn)
        notes = _drain(listener)
        assert notes, "INSERT should fire a notify on channel 'devices'"
        assert notes[0].channel == "devices"
        assert notes[0].payload.endswith(":" + TEST_MAC)
    finally:
        listener.close()


def test_last_seen_only_update_does_not_notify(conn):
    _insert_test_device(conn)
    listener = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    try:
        listener.execute("LISTEN devices")
        # Drain the INSERT notify so we start from a clean slate.
        _drain(listener)
        # Per-cycle refresh: bump last_seen only (and the discover cycle would
        # also bump generation) -- this MUST NOT wake classify.
        conn.execute(
            "UPDATE devices SET last_seen = NOW(), generation = generation + 1 "
            "WHERE mac = %s",
            (TEST_MAC,),
        )
        notes = _drain(listener)
        assert not notes, (
            "last_seen/generation-only UPDATE must NOT fire a notify, got: "
            f"{[(n.channel, n.payload) for n in notes]}"
        )
    finally:
        listener.close()


def test_latched_change_fires_notify(conn):
    _insert_test_device(conn)
    listener = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    try:
        listener.execute("LISTEN devices")
        _drain(listener)
        conn.execute(
            "UPDATE devices SET latched = %s::jsonb, generation = generation + 1 "
            "WHERE mac = %s",
            ('{"family": "tioga"}', TEST_MAC),
        )
        notes = _drain(listener)
        assert notes, "an UPDATE that changes latched MUST fire a notify"
        assert notes[0].channel == "devices"
    finally:
        listener.close()


def test_location_change_fires_notify(conn):
    _insert_test_device(conn)
    listener = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    try:
        listener.execute("LISTEN devices")
        _drain(listener)
        conn.execute(
            "UPDATE devices SET port = %s, generation = generation + 1 "
            "WHERE mac = %s",
            ("swp2", TEST_MAC),
        )
        notes = _drain(listener)
        assert notes, "an UPDATE that changes switch/port MUST fire a notify"
    finally:
        listener.close()


def test_kind_change_fires_notify(conn):
    _insert_test_device(conn)
    listener = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    try:
        listener.execute("LISTEN devices")
        _drain(listener)
        conn.execute(
            "UPDATE devices SET kind = %s, generation = generation + 1 "
            "WHERE mac = %s",
            ("host", TEST_MAC),
        )
        notes = _drain(listener)
        assert notes, "an UPDATE that changes kind MUST fire a notify"
    finally:
        listener.close()
