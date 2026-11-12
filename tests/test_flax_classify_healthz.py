"""Tests for flax_classify.healthz.HealthState."""

import time


def test_healthz_ok_when_recent_cycle():
    from flax_classify.healthz import HealthState

    h = HealthState(stale_secs=30)
    h.record_cycle_done(written=2, deleted=0, skipped=0)
    payload = h.snapshot()
    assert payload["status"] == "ok"
    assert payload["last_cycle"]["written"] == 2


def test_healthz_stale_when_cycle_too_old():
    from flax_classify.healthz import HealthState

    h = HealthState(stale_secs=0.01)
    h.record_cycle_done(written=1, deleted=0, skipped=0)
    time.sleep(0.05)
    assert h.snapshot()["status"] == "stale"


def test_healthz_never_run_is_starting():
    from flax_classify.healthz import HealthState
    h = HealthState(stale_secs=30)
    assert h.snapshot()["status"] == "starting"
