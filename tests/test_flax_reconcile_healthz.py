"""Tests for flax_reconcile.healthz.HealthState.

flax-reconcile's cycle returns {"steered", "refused", "enqueued", "kicked",
"mismatches", "circuit_open", "reclaimed"}; record_cycle_done stores all of
them so /healthz exposes the full last-cycle summary. circuit_open and
reclaimed default to 0 for older callers. record_cycle_done is invoked as
record_cycle_done(**summary) in __main__, so every summary key MUST be an
accepted kwarg -- a missing one raises TypeError (the circuit_open bug).
"""

import time


def _full_summary(**over):
    base = {"steered": 0, "refused": 0, "enqueued": 0, "kicked": 0,
            "mismatches": 0, "circuit_open": 0, "reclaimed": 0}
    base.update(over)
    return base


def test_healthz_records_reclaimed():
    from flax_reconcile.healthz import HealthState

    h = HealthState(stale_secs=30)
    h.record_cycle_done(**_full_summary(reclaimed=13, kicked=2))
    payload = h.snapshot()
    assert payload["status"] == "ok"
    assert payload["last_cycle"]["reclaimed"] == 13
    assert payload["last_cycle"]["kicked"] == 2


def test_healthz_reclaimed_defaults_to_zero():
    from flax_reconcile.healthz import HealthState

    h = HealthState(stale_secs=30)
    # older caller: no reclaimed kwarg (and no circuit_open) still works.
    h.record_cycle_done(steered=0, refused=0, enqueued=0, kicked=0,
                        mismatches=0)
    assert h.snapshot()["last_cycle"]["reclaimed"] == 0


def test_healthz_accepts_full_cycle_summary_kwargs():
    from flax_reconcile.healthz import HealthState

    # Mirrors record_cycle_done(**summary): every run_one_cycle key must be a
    # valid kwarg or this raises TypeError.
    h = HealthState(stale_secs=30)
    h.record_cycle_done(**_full_summary())
    assert h.snapshot()["status"] == "ok"


def test_healthz_never_run_is_starting():
    from flax_reconcile.healthz import HealthState

    h = HealthState(stale_secs=30)
    assert h.snapshot() == {"status": "starting"}


def test_healthz_stale_when_cycle_too_old():
    from flax_reconcile.healthz import HealthState

    h = HealthState(stale_secs=0.01)
    h.record_cycle_done(**_full_summary())
    time.sleep(0.05)
    assert h.snapshot()["status"] == "stale"
