"""Task 5: status-dashboard builders (pure) + partial/index render tests."""
from fastapi.testclient import TestClient
from unittest.mock import patch

from flax_control import dashboard_view


def _h(consumer, state):
    return {"consumer": consumer, "state": state, "consumed_at": None, "detail": None,
            "source": None, "generation": None}


def test_pipeline_all_healthy_is_operational():
    health = [_h(c, "healthy") for c in
              ("flax-discover", "flax-switch-sense", "flax-observe", "flax-classify", "flax-reconcile")]
    m = dashboard_view.build_pipeline(health)
    assert m["overall"] == "operational"
    # 5 services + the control stage itself
    assert [s["key"] for s in m["stages"]][-1] == "control"
    assert len(m["stages"]) == 6


def test_pipeline_failed_is_critical():
    health = [_h("flax-discover", "healthy"), _h("flax-classify", "failed")]
    m = dashboard_view.build_pipeline(health)
    assert m["overall"] == "critical"


def test_pipeline_stale_is_degraded():
    health = [_h("flax-observe", "stale")]
    m = dashboard_view.build_pipeline(health)
    assert m["overall"] == "degraded"


def test_pipeline_degraded_state_is_degraded():
    """A consumer whose sources partially failed reads state 'degraded' (amber),
    which rolls the whole pipeline up to degraded, not operational. All five
    services are present (no 'missing' defaults) so degraded is what's actually
    ranked."""
    services = ("flax-discover", "flax-switch-sense", "flax-observe",
                "flax-classify", "flax-reconcile")
    health = [_h(c, "healthy") for c in services]
    health[1] = _h("flax-switch-sense", "degraded")
    m = dashboard_view.build_pipeline(health)
    assert m["overall"] == "degraded"
    ss = next(s for s in m["stages"] if s["service"] == "flax-switch-sense")
    assert ss["state"] == "degraded"


def test_pipeline_failed_outranks_degraded():
    """A full failure anywhere still dominates a degraded elsewhere -> critical."""
    health = [_h("flax-switch-sense", "degraded"), _h("flax-classify", "failed")]
    m = dashboard_view.build_pipeline(health)
    assert m["overall"] == "critical"


def test_role_lanes_sum_per_role():
    lanes = dashboard_view.build_role_lanes(
        [("triage", "port", 12), ("post", "prefix", 7), ("post", "port", 3)])
    by = {l["role"]: l["dut_count"] for l in lanes}
    assert by == {"triage": 12, "post": 10}


def _patch_index(mp):
    mp.setattr("flax_control.queries.switches", lambda: [{"switch": "s", "reachable": True}])
    mp.setattr("flax_control.queries.consumer_acks", lambda: [])
    mp.setattr("flax_control.queries.events", lambda **k: [])
    mp.setattr("flax_control.queries.observe_state_all", lambda: {})
    mp.setattr("flax_control.queries.desired_by_role_kind", lambda: [("triage", "port", 5)])
    mp.setattr("flax_control.queries.leases", lambda: [])
    mp.setattr("flax_control.queries.reservations", lambda: [])


def test_index_renders_hero(monkeypatch):
    from flax_control.app import app
    _patch_index(monkeypatch)
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert b"System status" in resp.content
    assert b'hx-get="/partials/pipeline"' in resp.content


def test_partial_pipeline_route():
    from flax_control.app import app
    with patch("flax_control.queries.consumer_acks", return_value=[]):
        resp = TestClient(app).get("/partials/pipeline")
    assert resp.status_code == 200
    assert b"Discover" in resp.content and b"Control" in resp.content


def test_partial_events_route():
    from flax_control.app import app
    with patch("flax_control.queries.events", return_value=[]):
        resp = TestClient(app).get("/partials/events")
    assert resp.status_code == 200
