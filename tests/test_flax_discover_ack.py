# tests/test_flax_discover_ack.py
"""flax-discover wires the consumer_acks ledger each cycle (Task 3).

The daemon loop is an infinite while True, so we unit-test the extracted
helpers: _ack_action (pure decision), _ack_cycle (success-path write_ack,
wrapped so it can never crash the cycle) and _ack_failed (except-path).
write_ack is imported at module top so a test can monkeypatch
flax_discover.__main__.write_ack.
"""
import flax_discover.__main__ as m


def test_ack_action_applied_when_written():
    assert m._ack_action({"written": 3, "superseded": 0}) == "applied"


def test_ack_action_noop_when_nothing_written():
    assert m._ack_action({"written": 0, "superseded": 0}) == "noop"


def test_ack_cycle_calls_write_ack_applied(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append((a, kw)))
    pool = object()
    m._ack_cycle(pool, 7, {"written": 2, "superseded": 0})
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (pool, "flax-discover", "observe_state", 7, "applied")


def test_ack_cycle_calls_write_ack_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append(a))
    m._ack_cycle(object(), 1, {"written": 0})
    assert calls[0][1:] == ("flax-discover", "observe_state", 1, "noop")


def test_ack_cycle_swallows_write_ack_error(monkeypatch):
    """A write_ack failure must NOT crash the cycle."""
    def _boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "write_ack", _boom)
    # Must not raise.
    m._ack_cycle(object(), 1, {"written": 1})


def test_ack_failed_calls_write_ack_failed(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append((a, kw)))
    m._ack_failed(object(), 4, ValueError("boom"))
    args, kwargs = calls[0]
    assert args[1:] == ("flax-discover", "observe_state", 4, "failed")
    assert kwargs["detail"].startswith("boom")
