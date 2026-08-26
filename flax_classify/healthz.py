"""Tiny healthz server for flax-classify. Mirrors flax_switch_sense.healthz."""
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

    def record_cycle_done(self, *, written: int, deleted: int, skipped: int,
                          written_desired: int = 0, purged: int = 0):
        with self._lock:
            self._last_cycle_ts = time.monotonic()
            self._last_cycle = {"written": written, "deleted": deleted,
                                "skipped": skipped,
                                "written_desired": written_desired,
                                "purged": purged}

    def snapshot(self) -> dict:
        with self._lock:
            if self._last_cycle_ts is None:
                return {"status": "starting"}
            age = time.monotonic() - self._last_cycle_ts
            status = "ok" if age <= self._stale_secs else "stale"
            return {"status": status,
                    "age_secs": round(age, 1),
                    "last_cycle": self._last_cycle}


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
