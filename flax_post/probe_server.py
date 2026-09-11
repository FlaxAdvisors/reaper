# flax_post/probe_server.py
"""Tiny loopback control endpoint for the daemons that have no uvicorn app
(biosd, nicd): POST /probe/<port> -> probe_fn(port). The slot ladder calls it
when a host becomes ssh-reachable (spec 2026-09-11 post-slot-ladder §7).
Stdlib only; runs in a daemon thread; never raises into the caller."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("flax-post.probe-server")


class ProbeServer:
    def __init__(self, host, port, probe_fn):
        self._probe_fn = probe_fn
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("probe-server: " + fmt, *args)

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if not self.path.startswith("/probe/"):
                    return self._send(404, {"ok": False, "reason": "not found"})
                port_name = self.path[len("/probe/"):].strip("/")
                try:
                    row = srv._probe_fn(port_name)
                except Exception as e:
                    log.exception("probe %s failed", port_name)
                    return self._send(500, {"ok": False, "reason": str(e)})
                if row is None:
                    return self._send(404, {"ok": False, "reason": "unknown port"})
                if row.get("ok") is False:
                    return self._send(500, row)
                return self._send(200, {"ok": True, **row})

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="probe-server", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
