# tests/test_flax_post_app_qual.py
"""Viewer endpoints for Qualify: durable artifacts, live pass-through, re-run."""
from unittest.mock import patch
from fastapi.testclient import TestClient
from flax_post.app import app

client = TestClient(app)

SLOT = {"port": "et6b1", "bmc_mac": "aa:bb", "host_ip": "10.0.0.5",
        "bmc_ip": "10.0.0.6", "serial": "SN1", "order_no": "RRORD-003",
        "run_id": "r1"}


def test_artifact_list_and_fetch():
    with patch("flax_post.app._blade_slots", return_value=[SLOT]), \
         patch("flax_post.app.state.list_artifacts",
               return_value=[{"stage": "inventory", "name": "dmidecode", "kind": "raw", "bytes": 9}]), \
         patch("flax_post.app.state.get_artifact", return_value="Handle 0x1"):
        r = client.get("/api/v1/artifact", params={"port": "et6b1"})
        assert r.json()["artifacts"][0]["name"] == "dmidecode"
        r2 = client.get("/api/v1/artifact",
                        params={"port": "et6b1", "stage": "inventory", "name": "dmidecode"})
        assert r2.json()["content"] == "Handle 0x1"


def test_artifact_unknown_port_404():
    with patch("flax_post.app._blade_slots", return_value=[]):
        r = client.get("/api/v1/artifact", params={"port": "zzz"})
        assert r.status_code == 404


def test_qual_live_passthrough():
    class Agent:
        def stage(self, name): return {"name": name, "status": "running", "summary": {"pct": 40}}
    with patch("flax_post.app._blade_slots", return_value=[SLOT]), \
         patch("flax_post.app.QualClient", return_value=Agent()):
        r = client.get("/api/v1/qual/live", params={"port": "et6b1", "stage": "iperf"})
        assert r.json()["status"] == "running"


def test_qual_live_unreachable():
    from flax_post.qualclient import QualUnreachable
    class Agent:
        def stage(self, name): raise QualUnreachable("down")
    with patch("flax_post.app._blade_slots", return_value=[SLOT]), \
         patch("flax_post.app.QualClient", return_value=Agent()):
        r = client.get("/api/v1/qual/live", params={"port": "et6b1", "stage": "iperf"})
        assert r.json()["unreachable"] is True


def test_restart_drives_handshake():
    with patch("flax_post.app._blade_slots", return_value=[SLOT]), \
         patch("flax_post.app.host_qual.restart_target",
               return_value={"ok": True, "new_run_id": "r2"}) as rt:
        r = client.post("/api/v1/qual/restart", json={"port": "et6b1"})
        assert r.json()["new_run_id"] == "r2"
        assert rt.called
