"""Unit tests for the /healthz HTTP server."""
import threading
import time
import urllib.error
import urllib.request


def test_healthz_returns_ok_when_fetchers_recent():
    from flax_switch_sense.healthz import build_handler, FetcherStatus
    statuses = {
        "rabbit-fiesta": FetcherStatus(switch="rabbit-fiesta",
                                       last_polled=time.time(),
                                       last_error=None),
    }
    handler = build_handler(statuses, max_stale_secs=30)
    from http.server import HTTPServer
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            assert resp.status == 200
            body = resp.read().decode()
            assert "ok" in body
    finally:
        server.shutdown()


def test_healthz_returns_503_when_fetcher_stale():
    from flax_switch_sense.healthz import build_handler, FetcherStatus
    statuses = {
        "rabbit-fiesta": FetcherStatus(switch="rabbit-fiesta",
                                       last_polled=time.time() - 120,
                                       last_error="eAPI timeout"),
    }
    handler = build_handler(statuses, max_stale_secs=30)
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
