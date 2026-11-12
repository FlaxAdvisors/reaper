# tests/test_flax_reconcile_listen.py
"""Debouncer coalescing test — pure, no DB required."""
import time

from flax_reconcile.listen import Debouncer


def test_debouncer_coalesces(monkeypatch):
    fired = []
    d = Debouncer(target=lambda: fired.append(1), debounce_secs=0.05)
    d.start()
    for _ in range(5):
        d.ping()
    time.sleep(0.2)
    assert fired == [1]  # five pings -> one fire


def test_debouncer_fires_once_after_quiet_period():
    fired = []
    d = Debouncer(target=lambda: fired.append(1), debounce_secs=0.05)
    d.start()
    d.ping()
    time.sleep(0.2)
    assert len(fired) == 1


def test_debouncer_stops_cleanly():
    fired = []
    d = Debouncer(target=lambda: fired.append(1), debounce_secs=0.05)
    d.start()
    d.ping()
    d.stop()
    time.sleep(0.2)
    # After stop, the pending timer is cancelled — no fire.
    assert fired == []


def test_debouncer_channels_constant():
    from flax_reconcile.listen import CHANNELS
    assert set(CHANNELS) == {
        "lease_events",
        "kea_hosts_change",
        "desired_port",
        "reconcile_requests",
    }
    assert len(CHANNELS) == 4
