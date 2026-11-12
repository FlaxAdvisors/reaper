"""flax-observe wires the consumer_acks ledger once per supervisor cycle (Task 4).

The supervisor loop is an effectively-infinite `while not stop_event.wait(...)`
loop, so -- like Task 3's central services -- we unit-test the extracted
module-level helpers rather than running the loop:

  _ack_observe(pool, gen)         -> success-path write_ack (applied), wrapped
                                     so a ledger failure can never break observe
  _ack_observe_failed(pool, gen, exc) -> except-path write_ack (failed)

CRITICAL: the ack is emitted ONLY from the central supervisor loop, NOT from
the per-port PortWorker cycle (which would put 58 writers on one row). These
helpers ack consumer=flax-observe / source=switch_facts.

write_ack is imported at module top so a test can monkeypatch
flax_observe.__main__.write_ack.
"""
import flax_observe.__main__ as m


def test_ack_observe_calls_write_ack_applied(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append((a, kw)))
    pool = object()
    m._ack_observe(pool, 5)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (pool, "flax-observe", "switch_facts", 5, "applied")


def test_ack_observe_swallows_write_ack_error(monkeypatch):
    """A write_ack failure must NOT crash the supervisor loop."""
    def _boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "write_ack", _boom)
    # Must not raise.
    m._ack_observe(object(), 1)


def test_ack_observe_failed_calls_write_ack_failed(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append((a, kw)))
    m._ack_observe_failed(object(), 4, ValueError("boom"))
    args, kwargs = calls[0]
    assert args[1:] == ("flax-observe", "switch_facts", 4, "failed")
    assert kwargs["detail"].startswith("boom")


def test_ack_observe_failed_helper_propagates_write_ack_error(monkeypatch):
    """The failed-ack helper itself is NOT internally guarded (unlike the
    success helper), so the supervisor loop must wrap its call. This documents
    that the helper does propagate -- making the loop-level guard load-bearing
    so the best-effort failed-ack can never add a new crash path."""
    import pytest

    def _boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "write_ack", _boom)
    with pytest.raises(RuntimeError):
        m._ack_observe_failed(object(), 1, ValueError("orig"))
