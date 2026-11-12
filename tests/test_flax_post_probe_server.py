"""probe_server: the stdlib POST /probe/<port> control endpoint for biosd/nicd."""
import json
import urllib.request
from flax_post import probe_server


def _post(url):
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def test_probe_server_routes_to_probe_fn():
    calls = []
    def fn(port):
        calls.append(port)
        return {"port": port, "phase": "up_to_date"} if port == "et6b1" else None
    srv = probe_server.ProbeServer("127.0.0.1", 0, fn)
    srv.start()
    try:
        base = "http://127.0.0.1:%d" % srv.port
        assert _post(base + "/probe/et6b1") == (200, {"ok": True, "port": "et6b1", "phase": "up_to_date"})
        assert _post(base + "/probe/et9b9")[0] == 404
        assert _post(base + "/other")[0] == 404
        assert calls == ["et6b1", "et9b9"]
    finally:
        srv.stop()


def test_probe_server_reports_probe_exceptions_as_500():
    def fn(port):
        raise RuntimeError("boom")
    srv = probe_server.ProbeServer("127.0.0.1", 0, fn)
    srv.start()
    try:
        code, body = _post("http://127.0.0.1:%d/probe/et6b1" % srv.port)
        assert code == 500 and body["ok"] is False and "boom" in body["reason"]
    finally:
        srv.stop()


def test_probe_server_passes_through_probe_fn_failure_row_as_500():
    def fn(port):
        return {"port": "et6b1", "ok": False, "reason": "x"}
    srv = probe_server.ProbeServer("127.0.0.1", 0, fn)
    srv.start()
    try:
        code, body = _post("http://127.0.0.1:%d/probe/et6b1" % srv.port)
        assert code == 500 and body == {"port": "et6b1", "ok": False, "reason": "x"}
    finally:
        srv.stop()


def test_probe_server_sheds_load_past_max_parallel():
    """ThreadingHTTPServer spawns a thread per request and probe_port runs the
    SSH probe inline; a full-rack ladder rollout is ~48 concurrent probes on
    top of the daemon's own 24-wide tick. Past the bound, shed with 503."""
    import threading
    gate = threading.Event()
    started = threading.Semaphore(0)

    def fn(port):
        started.release()
        gate.wait(5)
        return {"port": port, "phase": "up_to_date"}

    srv = probe_server.ProbeServer("127.0.0.1", 0, fn, max_parallel=1)
    srv.start()
    base = "http://127.0.0.1:%d/probe/et6b1" % srv.port
    out = {}
    try:
        t = threading.Thread(target=lambda: out.update(first=_post(base)), daemon=True)
        t.start()
        assert started.acquire(timeout=5)          # the first probe is inside probe_fn
        assert _post(base) == (503, {"ok": False, "reason": "busy"})
        gate.set()
        t.join(10)
        assert out["first"] == (200, {"ok": True, "port": "et6b1", "phase": "up_to_date"})
        # the permit is released: the next request is served normally
        assert _post(base)[0] == 200
    finally:
        gate.set()
        srv.stop()


def test_probe_server_releases_its_permit_when_the_probe_raises():
    def fn(port):
        raise RuntimeError("boom")
    srv = probe_server.ProbeServer("127.0.0.1", 0, fn, max_parallel=1)
    srv.start()
    try:
        base = "http://127.0.0.1:%d/probe/et6b1" % srv.port
        for _ in range(3):
            assert _post(base)[0] == 500          # never 503: the permit came back
    finally:
        srv.stop()
