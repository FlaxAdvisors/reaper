"""LISTEN loop for flax-reconcile with a debounced re-run.

Listens on four channels:
  - lease_events       : fresh Kea/dnsmasq DHCP leases
  - kea_hosts_change   : classify changed a reservation in kea.hosts
  - desired_port       : classify wrote a new desired_port row
  - reconcile_requests : operator or auto enqueued a work item

Any notify on any of these channels indicates that a reconcile cycle may have
new work. A busy moment fires many NOTIFYs back-to-back; Debouncer coalesces
them into one cycle per debounce window so we don't burn DB sessions re-running
the same work.

Debouncer is a copy of flax_discover.listen.Debouncer (same image, but we copy
rather than import to avoid the dependency being load-order sensitive).
"""
import logging
import threading

import psycopg

log = logging.getLogger("flax-reconcile.listen")

CHANNELS = ("lease_events", "kea_hosts_change", "desired_port", "reconcile_requests")


class Debouncer:
    """Coalesces N pings within `debounce_secs` into one call to target.

    Copied verbatim from flax_discover.listen.Debouncer (both are in the same
    flax-control image; copying avoids subtle import-order coupling between
    discover and reconcile).
    """

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
        previously-scheduled call so rapid pings coalesce correctly."""
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
    """Persistent connection that LISTENs on all four channels and pings the
    debouncer on each notification. Runs forever; caller wraps in a
    thread + reconnect loop.
    """
    conn = psycopg.connect(conninfo, autocommit=True)
    for ch in CHANNELS:
        conn.execute("LISTEN " + ch)
    log.info("LISTEN %s active", " + ".join(CHANNELS))
    for _ in conn.notifies():
        debouncer.ping()
