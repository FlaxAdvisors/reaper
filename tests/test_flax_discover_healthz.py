"""Tests for flax_discover.healthz.HealthState.

flax-discover's cycle returns {"written": N, "superseded": M}, so
record_cycle_done takes `written` plus an optional `superseded` (defaults to 0
for older callers). Otherwise this mirrors flax_classify.healthz:
time.monotonic internally, status starting -> ok -> stale.
"""

import time


def test_healthz_ok_when_recent_cycle():
    from flax_discover.healthz import HealthState

    h = HealthState(stale_secs=30)
    h.record_cycle_done(written=2, superseded=1)
    payload = h.snapshot()
    assert payload["status"] == "ok"
    assert payload["last_cycle"]["written"] == 2
    assert payload["last_cycle"]["superseded"] == 1


def test_healthz_superseded_defaults_to_zero():
    from flax_discover.healthz import HealthState

    h = HealthState(stale_secs=30)
    h.record_cycle_done(written=5)  # older caller, no superseded kwarg
    assert h.snapshot()["last_cycle"]["superseded"] == 0


def test_healthz_records_swept_count():
    from flax_discover.healthz import HealthState

    # The cycle summary gained a `swept` count (devices vacancy GC) and
    # __main__ forwards the WHOLE summary via record_cycle_done(**summary),
    # so the signature MUST accept `swept` -- regression guard for the
    # TypeError that broke flax-discover health reporting on 0.9.45.
    h = HealthState(stale_secs=30)
    h.record_cycle_done(**{"written": 3, "superseded": 1, "swept": 2})
    last = h.snapshot()["last_cycle"]
    assert last["written"] == 3
    assert last["superseded"] == 1
    assert last["swept"] == 2


def test_healthz_swept_defaults_to_zero():
    from flax_discover.healthz import HealthState

    h = HealthState(stale_secs=30)
    h.record_cycle_done(written=1)  # older caller, no swept kwarg
    assert h.snapshot()["last_cycle"]["swept"] == 0


def test_healthz_stale_when_cycle_too_old():
    from flax_discover.healthz import HealthState

    h = HealthState(stale_secs=0.01)
    h.record_cycle_done(written=1)
    time.sleep(0.05)
    assert h.snapshot()["status"] == "stale"


def test_healthz_never_run_is_starting():
    from flax_discover.healthz import HealthState

    h = HealthState(stale_secs=30)
    assert h.snapshot()["status"] == "starting"
