"""Tiny HTTP server exposing /healthz for keepalived chk_services."""
import dataclasses
import json
import time
from http.server import BaseHTTPRequestHandler
from typing import Optional


@dataclasses.dataclass
class FetcherStatus:
    switch: str
    last_polled: Optional[float]   # unix timestamp
    last_error: Optional[str]


def build_handler(statuses: dict[str, FetcherStatus], *, max_stale_secs: float):
    """Return a BaseHTTPRequestHandler subclass that reports health based on
    the supplied per-switch fetcher statuses dict (live reference, polled at
    request time)."""

    class HealthzHandler(BaseHTTPRequestHandler):
        # Quiet -- stdlib's default logs every request to stderr
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path != "/healthz":
                self.send_error(404)
                return
            now = time.time()
            stale = []
            errors = []
            for s in statuses.values():
                if s.last_polled is None:
                    stale.append({"switch": s.switch, "reason": "never_polled"})
                elif now - s.last_polled > max_stale_secs:
                    stale.append({"switch": s.switch,
                                  "reason": f"last_polled_{int(now - s.last_polled)}s_ago"})
                if s.last_error:
                    errors.append({"switch": s.switch, "error": s.last_error})
            healthy = not stale
            body = {"status": "ok" if healthy else "fail",
                    "stale": stale, "errors": errors}
            self.send_response(200 if healthy else 503)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write((json.dumps(body) + "\n").encode())

    return HealthzHandler
