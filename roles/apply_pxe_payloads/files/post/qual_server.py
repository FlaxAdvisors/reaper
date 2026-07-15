#!/usr/bin/env python3
"""Read-only HTTP contract surface for the qualification agent (+ one /restart
control route). Stdlib http.server only; imports under Python 3.6.
"""
import json
from http.server import BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer   # py3.7+
except ImportError:                                # py3.6: build it from stdlib parts
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True


def make_handler(battery):
    class QualHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass                                            # quiet

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, text, code=200):
            body = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _404(self):
            self._json({"error": "not found"}, 404)

        def do_GET(self):
            p = self.path.split("?", 1)[0].rstrip("/") or "/"
            parts = [x for x in p.split("/") if x]
            if p == "/health":
                return self._json(battery.health())
            if p == "/status":
                return self._json(battery.status())
            if p == "/stages":
                return self._json(battery.stages_list())
            if len(parts) == 2 and parts[0] == "stage":
                st = battery.stage(parts[1])
                return self._json(st) if st else self._404()
            if len(parts) == 3 and parts[0] == "stage":
                text = battery.artifact(parts[1], parts[2])
                return self._text(text) if text is not None else self._404()
            return self._404()

        def do_POST(self):
            p = self.path.split("?", 1)[0].rstrip("/")
            if p == "/restart":
                return self._json(battery.request_restart())
            if p == "/restart/ack":
                return self._json(battery.ack_restart())
            return self._404()
    return QualHandler


def serve(battery, host="0.0.0.0", port=8087):
    return ThreadingHTTPServer((host, port), make_handler(battery))
