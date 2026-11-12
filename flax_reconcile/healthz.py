"""Tiny healthz server for flax-reconcile. Mirrors flax_discover/healthz.py.

flax-reconcile's cycle returns {"steered", "refused", "enqueued", "kicked",
"mismatches"} (five keys). record_cycle_done stores all five so the /healthz
endpoint exposes the full last-cycle summary for alerting and debugging.
Otherwise identical shape to flax_discover.healthz: status starts at "starting"
then transitions to "ok" / "stale" based on age vs stale_secs.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class HealthState:
    """Tracks last cycle outcome + freshness."""

    def __init__(self, stale_secs: float):
        self._lock = threading.Lock()
        self._last_cycle_ts = None
        self._last_cycle = None
        self._stale_secs = stale_secs

    def record_cycle_done(self, *, steered: int, refused: int,
                          enqueued: int, kicked: int, mismatches: int,
                          circuit_open: int = 0, reclaimed: int = 0):
        """Record the summary keys from Reconciler.run_one_cycle.

        circuit_open is the count of macs currently held off by the convergence
        circuit-breaker's backoff window this cycle (useful operator signal).
        reclaimed is the count of crash-stranded 'claimed' rows reset back to
        'pending' this cycle (a nonzero value flags a recent crash/restart).
        Both default to 0 for older callers that don't pass them.
        """
        with self._lock:
            self._last_cycle_ts = time.monotonic()
            self._last_cycle = {
                "steered": steered,
                "refused": refused,
                "enqueued": enqueued,
                "kicked": kicked,
                "mismatches": mismatches,
                "circuit_open": circuit_open,
                "reclaimed": reclaimed,
            }

    def snapshot(self) -> dict:
        with self._lock:
            if self._last_cycle_ts is None:
                return {"status": "starting"}
            age = time.monotonic() - self._last_cycle_ts
            status = "ok" if age <= self._stale_secs else "stale"
            return {
                "status": status,
                "age_secs": round(age, 1),
                "last_cycle": self._last_cycle,
            }


def serve(state: HealthState, port: int):
    """Block forever serving /healthz from the given HealthState."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):  # silence stdout spam
            pass

        def do_GET(self):
            if self.path != "/healthz":
                self.send_response(404)
                self.end_headers()
                return
            payload = state.snapshot()
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
