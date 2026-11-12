"""Tests for flax_observe.healthz."""
import threading
import time
import urllib.error
import urllib.request


def test_healthz_ok_when_workers_recent():
    from flax_observe.healthz import build_handler, WorkerStatus
    statuses = {("rabbit-lorax", "Et6/1"):
                WorkerStatus(switch="rabbit-lorax", port="Et6/1",
                             last_cycle=time.time(), last_error=None)}
    handler = build_handler(statuses, max_stale_secs=60)
    from http.server import HTTPServer
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            assert resp.status == 200
            assert b"ok" in resp.read()
    finally:
        server.shutdown()


def test_healthz_503_when_workers_stale():
    from flax_observe.healthz import build_handler, WorkerStatus
    statuses = {("rabbit-lorax", "Et6/1"):
                WorkerStatus(switch="rabbit-lorax", port="Et6/1",
                             last_cycle=time.time() - 300, last_error="ipmi timeout")}
    handler = build_handler(statuses, max_stale_secs=60)
    from http.server import HTTPServer
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2)
            assert False, "expected HTTPError"
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        server.shutdown()
