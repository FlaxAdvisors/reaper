"""Unit tests for the work-record retention caller (no DB; the policy itself
is tested in test_flax_post_work_records_db.py against a real database)."""
from unittest import mock

import pytest

from flax_post import retention


@pytest.fixture(autouse=True)
def _reset_last_run():
    """The module keeps _last_run across calls; the tests use explicit now=
    values that move backwards, so isolate them from each other."""
    retention._last_run = None
    yield
    retention._last_run = None


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        return mock.Mock(fetchall=lambda: self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Pool:
    def __init__(self, rows=None):
        self.conn = _Conn(rows if rows is not None else
                          [("records", 6), ("empty_serial_records", 1),
                           ("dut_rows", 2), ("capped_remaining", 0)])

    def connection(self):
        return self.conn


def test_run_retention_calls_the_policy_and_returns_counts():
    pool = _Pool()
    out = retention.run_retention(pool, keep=5, max_deletes=20000, enabled=True,
                                  interval_secs=3600, now=1000.0)
    assert out == {"records": 6, "empty_serial_records": 1, "dut_rows": 2,
                   "capped_remaining": 0}
    sql, args = pool.conn.calls[0]
    assert "work_records_retain" in sql
    assert args == (5, False, 20000)          # dry_run False: the sweep deletes


def test_run_retention_disabled_is_a_no_op():
    pool = _Pool()
    assert retention.run_retention(pool, keep=5, max_deletes=1, enabled=False,
                                   interval_secs=0, now=1000.0) is None
    assert pool.conn.calls == []


def test_run_retention_respects_the_interval():
    pool = _Pool()
    first = retention.run_retention(pool, keep=5, max_deletes=1, enabled=True,
                                    interval_secs=3600, now=1000.0)
    second = retention.run_retention(pool, keep=5, max_deletes=1, enabled=True,
                                     interval_secs=3600, now=1000.0 + 60)
    third = retention.run_retention(pool, keep=5, max_deletes=1, enabled=True,
                                    interval_secs=3600, now=1000.0 + 3601)
    assert first is not None and second is None and third is not None
    assert len(pool.conn.calls) == 2


def test_run_retention_logs_one_line(caplog):
    caplog.set_level("INFO", logger="flax-post.retention")
    retention.run_retention(_Pool(), keep=5, max_deletes=20000, enabled=True,
                            interval_secs=0, now=2000.0)
    assert [r.getMessage() for r in caplog.records] == [
        "records-retention kept=5 records=6 empty=1 duts=2 capped=0"]


def test_run_retention_logs_nothing_when_there_was_nothing_to_delete(caplog):
    caplog.set_level("INFO", logger="flax-post.retention")
    pool = _Pool(rows=[("records", 0), ("empty_serial_records", 0),
                       ("dut_rows", 0), ("capped_remaining", 0)])
    out = retention.run_retention(pool, keep=5, max_deletes=20000, enabled=True,
                                  interval_secs=0, now=3000.0)
    assert out == {"records": 0, "empty_serial_records": 0, "dut_rows": 0,
                   "capped_remaining": 0}
    assert caplog.records == []


def test_run_retention_propagates_db_errors_for_the_caller_to_swallow():
    class Boom(_Pool):
        def connection(self):
            raise RuntimeError("db down")
    with pytest.raises(RuntimeError):
        retention.run_retention(Boom(), keep=5, max_deletes=1, enabled=True,
                                interval_secs=0, now=4000.0)


def test_a_failed_sweep_does_not_consume_the_interval():
    """A raising sweep must not block the next pass for a whole interval."""
    class _Boom:
        def connection(self):
            raise RuntimeError("db down")
    with pytest.raises(RuntimeError):
        retention.run_retention(_Boom(), keep=5, max_deletes=1, enabled=True,
                                interval_secs=3600, now=1000.0)
    pool = _Pool()
    assert retention.run_retention(pool, keep=5, max_deletes=1, enabled=True,
                                   interval_secs=3600, now=1001.0) is not None


def test_bogus_numeric_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("FLAX_RECORDS_KEEP", "five")
    monkeypatch.setenv("FLAX_RECORDS_RETENTION_INTERVAL_SECS", "")
    monkeypatch.setenv("FLAX_RECORDS_MAX_DELETES", "lots")
    import importlib
    mod = importlib.reload(retention)          # must not raise
    try:
        assert (mod.KEEP, mod.INTERVAL_SECS, mod.MAX_DELETES) == (5, 3600.0, 20000)
    finally:
        monkeypatch.undo()
        importlib.reload(retention)


def test_module_defaults_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("FLAX_RECORDS_RETENTION_ENABLED", "true")
    monkeypatch.setenv("FLAX_RECORDS_KEEP", "7")
    monkeypatch.setenv("FLAX_RECORDS_RETENTION_INTERVAL_SECS", "60")
    monkeypatch.setenv("FLAX_RECORDS_MAX_DELETES", "10")
    import importlib
    mod = importlib.reload(retention)
    try:
        assert (mod.ENABLED, mod.KEEP, mod.INTERVAL_SECS, mod.MAX_DELETES) == (True, 7, 60, 10)
    finally:
        monkeypatch.undo()
        importlib.reload(retention)


def test_defaults_are_off_and_conservative():
    assert (retention.ENABLED, retention.KEEP, retention.INTERVAL_SECS,
            retention.MAX_DELETES) == (False, 5, 3600, 20000)
