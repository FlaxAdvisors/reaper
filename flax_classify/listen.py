"""LISTEN switch_facts + observe_state with a debounced re-run.

flax-switch-sense and flax-observe both NOTIFY on table change; a busy
poll cycle can fire 80+ NOTIFYs back-to-back. Coalesce them into one
classify cycle per debounce window (default 1.5s) so we don't burn DB
sessions re-running the same work.
"""
import logging
import threading

import psycopg


log = logging.getLogger("flax-classify.listen")


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
    conn.execute("LISTEN switch_facts")
    conn.execute("LISTEN observe_state")
    conn.execute("LISTEN devices")
    log.info("LISTEN switch_facts + observe_state + devices active")
    for _ in conn.notifies():
        debouncer.ping()
