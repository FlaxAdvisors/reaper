"""LISTEN observe_state + lease_events with a debounced re-run.

flax-observe NOTIFYs observe_state when a port's resolved identity changes
(a new device showing up), and Kea/dnsmasq NOTIFY lease_events on a fresh
lease. Either signals that a discover cycle may have new work. A busy moment
can fire many NOTIFYs back-to-back; coalesce them into one discover cycle per
debounce window (default 1.5s) so we don't burn DB sessions re-running the
same work.
"""
import logging
import threading

import psycopg


log = logging.getLogger("flax-discover.listen")


class Debouncer:
    """Coalesces N pings within `debounce_secs` into one call to target."""

    def __init__(self, target, debounce_secs: float):
        self._target = target
        self._debounce = debounce_secs
        self._lock = threading.Lock()
        self._timer = None
        self._stopped = False

    def start(self):
        self._stopped = False

    def stop(self):
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def ping(self):
        """Schedule a target() call in `debounce_secs`, replacing any
        previously-scheduled call so coalesces are accurate."""
        with self._lock:
            if self._stopped:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            self._timer = None
            if self._stopped:
                return
        try:
            self._target()
        except Exception:
            log.exception("debounced target raised")


def listen_loop(conninfo: str, debouncer: Debouncer):
    """Persistent connection that LISTENs to both channels and pings the
    debouncer on each notification. Runs forever; caller wraps in a
    thread + reconnect loop.
    """
    conn = psycopg.connect(conninfo, autocommit=True)
    conn.execute("LISTEN observe_state")
    conn.execute("LISTEN lease_events")
    log.info("LISTEN observe_state + lease_events active")
    for _ in conn.notifies():
        debouncer.ping()
