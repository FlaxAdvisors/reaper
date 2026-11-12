# tests/test_flax_classify_ack.py
"""flax-classify wires the consumer_acks ledger each cycle (Task 3)."""
import flax_classify.__main__ as m


def test_ack_action_applied_when_written():
    assert m._ack_action({"written": 1, "written_desired": 0}) == "applied"


def test_ack_action_applied_when_written_desired():
    assert m._ack_action({"written": 0, "written_desired": 2}) == "applied"


def test_ack_action_noop_when_nothing_written():
    assert m._ack_action({"written": 0, "written_desired": 0}) == "noop"


def test_ack_cycle_calls_write_ack_applied(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append(a))
    pool = object()
    m._ack_cycle(pool, 5, {"written": 0, "written_desired": 3})
    assert calls[0] == (pool, "flax-classify", "observe_state", 5, "applied")


def test_ack_cycle_calls_write_ack_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append(a))
    m._ack_cycle(object(), 2, {"written": 0, "written_desired": 0})
    assert calls[0][1:] == ("flax-classify", "observe_state", 2, "noop")


def test_ack_cycle_swallows_write_ack_error(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "write_ack", _boom)
    m._ack_cycle(object(), 1, {"written": 1, "written_desired": 0})


def test_ack_failed_calls_write_ack_failed(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append((a, kw)))
    m._ack_failed(object(), 9, ValueError("kaboom"))
    args, kwargs = calls[0]
    assert args[1:] == ("flax-classify", "observe_state", 9, "failed")
    assert kwargs["detail"].startswith("kaboom")
