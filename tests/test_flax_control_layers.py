"""Phase-5 layer health pages: panel builders + routes."""
import datetime

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


_NOW = datetime.datetime(2026, 7, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _health(consumer, state):
    return {"consumer": consumer, "state": state, "source": None,
            "generation": 1, "consumed_at": None, "detail": None}


def test_panel_shape():
    from flax_control.layers_view import panel
    p = panel("kea.hosts", 100, badge="ok", href="/reservations", hint="rows")
    assert p == {"label": "kea.hosts", "value": 100, "badge": "ok",
                 "href": "/reservations", "hint": "rows"}


def test_build_sensing_flags_unreachable_switch_and_stale_ack():
    from flax_control.layers_view import build_sensing
    model = build_sensing(
        [_health("flax-switch-sense", "healthy"),
         _health("flax-observe", "stale"),
         _health("flax-discover", "healthy")],
        switches_rows=[{"switch": "rabbit-test", "reachable": False,
                        "polled_at": "t", "port_count": 70}],
        observe_stats=(120, 30.0),
        lease_count=9)
    by_label = {p["label"]: p for p in model["panels"]}
    assert by_label["flax-observe ack"]["badge"] == "warn"
    assert by_label["flax-switch-sense ack"]["badge"] == "ok"
    assert by_label["switches reachable"]["badge"] == "warn"   # 0/1 reachable
    assert by_label["active leases"]["value"] == 9
    assert by_label["observe_state rows"]["value"] == 120


def test_build_policy_uncovered_warns():
    from flax_control.layers_view import build_policy
    registry = {"roles": [{"name": "post", "generation": 3,
                           "loaded_at": "t", "universe_summary": [],
                           "capabilities": {}, "policy": {},
                           "record_keys": []}], "catch_all_role": None}
    model = build_policy([_health("flax-classify", "healthy")], registry,
                         desired_rows=[("post", 88, "t")], uncovered_count=2)
    by_label = {p["label"]: p for p in model["panels"]}
    assert by_label["registry"]["value"] == "1 roles · gen 3"
    assert by_label["unassigned access ports"]["badge"] == "warn"
    assert by_label["desired (post)"]["value"] == 88


def test_build_actuation_freeze_and_convergence():
    from flax_control.layers_view import build_actuation
    fresh = _NOW - datetime.timedelta(seconds=30)
    model = {"ran": True, "converged": True, "latest_plan_ts": fresh,
             "planned_count": 0, "by_owner_action": [], "skipped": {},
             "skip_total": 0, "plan_rows": [], "events": [],
             "desired_summary": []}
    result = build_actuation([_health("flax-reconcile", "healthy")], model,
                             kea_count=100, cadence_rows=[("set_vlan", "ok", 4)],
                             flap_count=0, now=_NOW, stale_secs=180)
    by_label = {p["label"]: p for p in result["panels"]}
    assert by_label["materializer"]["badge"] == "ok"
    assert by_label["kea.hosts"]["value"] == 100
    assert by_label["reconcile actions (1h)"]["value"] == 4
    stale_result = build_actuation([], dict(model, latest_plan_ts=None,
                                            ran=False, converged=None),
                                   100, [], 0, _NOW, 180)
    assert {p["label"]: p for p in stale_result["panels"]}["materializer"]["badge"] == "err"


def test_layer_routes_render(client):
    with patch("flax_control.queries.consumer_acks", return_value=[]), \
         patch("flax_control.queries.switches", return_value=[]), \
         patch("flax_control.queries.observe_state_stats", return_value=(0, None)), \
         patch("flax_control.queries.active_lease_count", return_value=0), \
         patch("flax_control.queries.roles", return_value=[]), \
         patch("flax_control.queries.role_universe", return_value=[]), \
         patch("flax_control.queries.switch_ports_all", return_value=[]), \
         patch("flax_control.queries.desired_summary", return_value=[]), \
         patch("flax_control.queries.materializer_recent", return_value=[]), \
         patch("flax_control.queries.ownership_events_recent", return_value=[]), \
         patch("flax_control.queries.kea_hosts_count", return_value=0), \
         patch("flax_control.queries.reconcile_cadence", return_value=[]), \
         patch("flax_control.queries.intentional_flap_active", return_value=0), \
         patch("flax_control.queries.layer_anomalies", return_value=[]), \
         patch("flax_control.queries.events_for_services", return_value=[]), \
         patch("flax_control.config_map_view.catalogue_with_mtimes", return_value=[]):
        for path in ("/layers/sensing", "/layers/policy", "/layers/actuation"):
            resp = client.get(path)
            assert resp.status_code == 200, path
