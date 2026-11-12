"""Task 4: layer-page enrichment — config-that-drives + per-service state +
recent anomalies + scoped events. Builders stay pure (no DB import); routes
fetch the extra rows and pass them in."""
import datetime

from flax_control import layers_view

_NOW = datetime.datetime(2026, 7, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _health(consumer, state, detail=None):
    return {"consumer": consumer, "state": state, "source": None,
            "generation": 1, "consumed_at": None, "detail": detail}


def test_build_sensing_returns_enriched_dict_shape():
    model = layers_view.build_sensing(
        [], switches_rows=[], observe_stats=(0, None), lease_count=0)
    assert set(model.keys()) == {"panels", "config", "anomalies", "events"}
    assert isinstance(model["panels"], list)
    assert model["config"] == []
    assert model["anomalies"] == []
    assert model["events"] == []


def test_build_sensing_passes_through_config_anomalies_events():
    cfg = [{"name": "geometry.json", "decides": "x",
            "readers": ["flax-classify"], "live_state": "live"}]
    anomalies = [{"when": "t", "source": "flax-observe", "what": "deferred"}]
    events = [{"ts": "t", "service": "flax-observe", "kind": "poll",
               "switch": None, "port": None, "mac": None, "payload": {}}]
    model = layers_view.build_sensing(
        [], switches_rows=[], observe_stats=(0, None), lease_count=0,
        config_rows=cfg, anomalies=anomalies, events=events)
    assert model["config"] == cfg
    assert model["anomalies"] == anomalies
    assert model["events"] == events


def test_ack_panel_includes_detail_reason():
    health = [_health("flax-observe", "failed",
                       detail={"reason": "switch unreachable",
                               "action": "deferred"})]
    model = layers_view.build_sensing(
        health, switches_rows=[], observe_stats=(0, None), lease_count=0)
    by_label = {p["label"]: p for p in model["panels"]}
    assert "switch unreachable" in by_label["flax-observe ack"]["value"]


def test_build_policy_returns_enriched_dict_shape():
    registry = {"roles": [], "catch_all_role": None}
    model = layers_view.build_policy(
        [], registry, desired_rows=[], uncovered_count=0)
    assert set(model.keys()) == {"panels", "config", "anomalies", "events"}
    assert isinstance(model["panels"], list)


def test_build_actuation_returns_enriched_dict_shape():
    shadow_model = {"converged": True, "latest_plan_ts": _NOW,
                    "planned_count": 0}
    model = layers_view.build_actuation(
        [], shadow_model, 0, [], 0, _NOW, 180)
    assert set(model.keys()) == {"panels", "config", "anomalies", "events"}
    assert isinstance(model["panels"], list)


