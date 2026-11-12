"""Unit test for the shared-shape write_ack consumer_acks helper.

write_ack is deliberately duplicated byte-for-byte across the 5 flax
services; we test one canonical copy (flax_observe.persistence).
"""
import re

from flax_observe.persistence import write_ack


class _FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


class _FakeCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def connection(self):
        return _FakeCtx(self.conn)


def test_write_ack_sql_and_params():
    pool = _FakePool()
    write_ack(pool, "flax-observe", "switch_facts", "7", "applied", detail="ok")

    assert len(pool.conn.calls) == 1
    sql, params = pool.conn.calls[0]

    # Upsert shape: ON CONFLICT high-water-mark via GREATEST.
    assert "ON CONFLICT (consumer, source)" in sql
    assert "GREATEST(consumer_acks.generation, EXCLUDED.generation)" in sql

    # Params in order: (consumer, source, int(generation), action, <iso ts>, detail)
    consumer, source, generation, action, consumed_at, detail = params
    assert consumer == "flax-observe"
    assert source == "switch_facts"
    assert generation == 7          # str "7" coerced via int()
    assert action == "applied"
    assert detail == "ok"
    # consumed_at is an ISO-8601 timestamp string.
    assert isinstance(consumed_at, str)
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", consumed_at)


def test_write_ack_coerces_float_generation_and_default_detail():
    pool = _FakePool()
    write_ack(pool, "flax-reconcile", "desired_port", 12.0, "noop")

    _, params = pool.conn.calls[0]
    assert params[2] == 12 and isinstance(params[2], int)
    assert params[5] is None        # detail defaults to None
