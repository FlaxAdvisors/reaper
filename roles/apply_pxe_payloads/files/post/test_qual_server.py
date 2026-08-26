import json, os, sys, threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.error import HTTPError
sys.path.insert(0, os.path.dirname(__file__))
from qual_server import make_handler
from qual_battery import Battery


def _server():
    b = Battery(runner=lambda a, t: (0, ""),
                stages=[{"name": "sdr-pre", "fn": lambda r: {
                    "verdict": "pass", "summary": {"lines": 3},
                    "artifacts": {"sdr": ("raw", "SENSOR ok")}}}],
                mac="aa:bb", serial="SN1")
    b.run()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(b))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return b, srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _get(url):
    return urlopen(url, timeout=5).read().decode()


def test_health_status_stages_shapes():
    b, srv, base = _server()
    try:
        h = json.loads(_get(base + "/health"))
        assert h["ok"] is True and h["run_id"] == b.run_id and h["mac"] == "aa:bb"
        s = json.loads(_get(base + "/status"))
        assert s["status"] == "done" and s["verdict"] == "pass" and s["total_n"] == 1
        stages = json.loads(_get(base + "/stages"))
        assert stages[0]["name"] == "sdr-pre" and stages[0]["status"] == "pass"
    finally:
        srv.shutdown()


def test_stage_and_artifact_text():
    b, srv, base = _server()
    try:
        st = json.loads(_get(base + "/stage/sdr-pre"))
        assert st["artifacts"][0]["name"] == "sdr" and st["summary"]["lines"] == 3
        assert _get(base + "/stage/sdr-pre/sdr") == "SENSOR ok"          # raw text, not JSON
    finally:
        srv.shutdown()


def test_unknown_paths_404():
    b, srv, base = _server()
    try:
        for path in ("/nope", "/stage/zzz", "/stage/sdr-pre/zzz"):
            try:
                urlopen(base + path, timeout=5); assert False, path
            except HTTPError as e:
                assert e.code == 404
    finally:
        srv.shutdown()


def test_restart_handshake_over_http():
    b, srv, base = _server()
    try:
        old = b.run_id
        r = json.loads(urlopen(Request(base + "/restart", method="POST"), timeout=5).read().decode())
        assert r["old_run_id"] == old and r["new_run_id"]
        assert json.loads(_get(base + "/health"))["state"] == "reset_pending"
        ack = json.loads(urlopen(Request(base + "/restart/ack", method="POST"), timeout=5).read().decode())
        assert ack["ok"] is True
    finally:
        srv.shutdown()
