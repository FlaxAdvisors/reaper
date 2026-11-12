"""Tests for flax_discover.listen.Debouncer."""

import time


def test_debouncer_coalesces_rapid_pings_into_one_call():
    """N pings within the debounce window -> one downstream call."""
    from flax_discover.listen import Debouncer

    calls = []
    d = Debouncer(target=lambda: calls.append(time.monotonic()),
                  debounce_secs=0.05)
    d.start()
    try:
        for _ in range(10):
            d.ping()
            time.sleep(0.005)  # 5ms apart -> all within 50ms window
        time.sleep(0.15)  # let the debounce timer fire
        assert len(calls) == 1
    finally:
        d.stop()


def test_debouncer_idle_does_not_call():
    from flax_discover.listen import Debouncer

    calls = []
    d = Debouncer(target=lambda: calls.append(1), debounce_secs=0.05)
    d.start()
    try:
        time.sleep(0.2)
        assert calls == []
    finally:
        d.stop()
