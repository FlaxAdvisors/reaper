"""Tiny HTTP /healthz for flax-observe (used by keepalived chk_services)."""
import dataclasses
import json
import time
from http.server import BaseHTTPRequestHandler
from typing import Optional


@dataclasses.dataclass
class WorkerStatus:
    switch: str
    port: str
    last_cycle: Optional[float]
    last_error: Optional[str]


def build_handler(statuses, *, max_stale_secs: float):
    class HealthzHandler(BaseHTTPRequestHandler):
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
                if s.last_cycle is None:
                    stale.append({"port": f"{s.switch}/{s.port}", "reason": "never_cycled"})
                elif now - s.last_cycle > max_stale_secs:
                    stale.append({"port": f"{s.switch}/{s.port}",
                                  "reason": f"last_cycle_{int(now - s.last_cycle)}s_ago"})
                if s.last_error:
                    errors.append({"port": f"{s.switch}/{s.port}", "error": s.last_error})
            healthy = not stale
            body = {"status": "ok" if healthy else "fail",
                    "stale": stale, "errors": errors,
                    "total_workers": len(statuses)}
            self.send_response(200 if healthy else 503)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write((json.dumps(body) + "\n").encode())

    return HealthzHandler
