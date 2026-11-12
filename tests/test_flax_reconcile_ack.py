# tests/test_flax_reconcile_ack.py
"""flax-reconcile wires the consumer_acks ledger each cycle (Task 3)."""
import flax_reconcile.__main__ as m


def test_ack_action_applied_when_steered():
    assert m._ack_action({"steered": 1, "enqueued": 0, "kicked": 0}) == "applied"


def test_ack_action_applied_when_enqueued():
    assert m._ack_action({"steered": 0, "enqueued": 2, "kicked": 0}) == "applied"


def test_ack_action_applied_when_kicked():
    assert m._ack_action({"steered": 0, "enqueued": 0, "kicked": 4}) == "applied"


def test_ack_action_noop_when_idle():
    assert m._ack_action({"steered": 0, "enqueued": 0, "kicked": 0}) == "noop"


def test_ack_cycle_calls_write_ack_applied(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append(a))
    pool = object()
    m._ack_cycle(pool, 11, {"steered": 0, "enqueued": 0, "kicked": 1})
    assert calls[0] == (pool, "flax-reconcile", "desired_port", 11, "applied")


def test_ack_cycle_calls_write_ack_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append(a))
    m._ack_cycle(object(), 3, {"steered": 0, "enqueued": 0, "kicked": 0})
    assert calls[0][1:] == ("flax-reconcile", "desired_port", 3, "noop")


def test_ack_cycle_swallows_write_ack_error(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "write_ack", _boom)
    m._ack_cycle(object(), 1, {"steered": 1, "enqueued": 0, "kicked": 0})


def test_ack_failed_calls_write_ack_failed(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda *a, **kw: calls.append((a, kw)))
    m._ack_failed(object(), 6, ValueError("splat"))
    args, kwargs = calls[0]
    assert args[1:] == ("flax-reconcile", "desired_port", 6, "failed")
    assert kwargs["detail"].startswith("splat")


# ---------------------------------------------------------------------------
# _load_eligible_sources (Task 1: registry capability lookup) -- a thin
# startup-read wrapper, same pattern as _load_vlan_parents/load_no_steer/
# load_redfish_creds: read once, delegate, let the caller pass the result
# straight into Reconciler(eligible_sources=...).
# ---------------------------------------------------------------------------
def test_load_eligible_sources_delegates_to_role_caps(monkeypatch):
    sentinel = frozenset({"triage", "legacy-import", ""})
    calls = []

    def _fake_read(pool):
        calls.append(pool)
        return sentinel

    monkeypatch.setattr(m.role_caps, "read_reconcile_eligible_sources", _fake_read)
    pool = object()
    assert m._load_eligible_sources(pool) is sentinel
    assert calls == [pool]


def test_load_eligible_sources_passes_through_none(monkeypatch):
    monkeypatch.setattr(m.role_caps, "read_reconcile_eligible_sources",
                        lambda pool: None)
    assert m._load_eligible_sources(object()) is None
