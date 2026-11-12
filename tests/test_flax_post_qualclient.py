"""qualclient: typed client for the node agent REST contract (fake transport)."""
import json
import pytest


def _transport(routes):
    """routes: {(method, path): (code, body_str)} -> a transport(method, url, timeout)."""
    def t(method, url, timeout):
        path = url.split("PORT", 1)[1] if "PORT" in url else url
        return routes[(method, path)]
    return t


def test_status_parses_json():
    from flax_post.qualclient import QualClient
    routes = {("GET", "/status"): (200, json.dumps(
        {"status": "running", "current": "iperf", "done_n": 6, "total_n": 11,
         "verdict": None, "pct": 54}))}
    c = QualClient("http://hostPORT", transport=_transport(routes))
    assert c.status()["current"] == "iperf"
    assert c.status()["pct"] == 54


def test_stage_artifact_returns_raw_text():
    from flax_post.qualclient import QualClient
    routes = {("GET", "/stage/inventory/dmidecode"): (200, "Handle 0x0001\nBIOS Information")}
    c = QualClient("http://hostPORT", transport=_transport(routes))
    assert c.stage_artifact("inventory", "dmidecode").startswith("Handle 0x0001")


def test_restart_then_ack():
    from flax_post.qualclient import QualClient
    routes = {("POST", "/restart"): (200, json.dumps({"old_run_id": "r1", "new_run_id": "r2"})),
              ("POST", "/restart/ack"): (200, json.dumps({"ok": True}))}
    c = QualClient("http://hostPORT", transport=_transport(routes))
    assert c.restart()["new_run_id"] == "r2"
    assert c.restart_ack()["ok"] is True


def test_non_2xx_raises_unreachable():
    from flax_post.qualclient import QualClient, QualUnreachable
    routes = {("GET", "/health"): (503, "")}
    c = QualClient("http://hostPORT", transport=_transport(routes))
    with pytest.raises(QualUnreachable):
        c.health()


def test_malformed_2xx_body_raises_unreachable():
    from flax_post.qualclient import QualClient, QualUnreachable
    routes = {("GET", "/status"): (200, "{not json")}
    c = QualClient("http://hostPORT", transport=_transport(routes))
    with pytest.raises(QualUnreachable):
        c.status()
