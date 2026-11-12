"""Phase-5 per-role lane cards: pure shaping + freeze detector + route."""
import datetime

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


_NOW = datetime.datetime(2026, 7, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _registry():
    from flax_control.roles_view import build_registry
    return build_registry(
        [("post", {"record_keys": ["customer", "order"]}, 3, "t"),
         ("triage", {"record_keys": ["mac", "serial"]}, 3, "t")],
        [("post", "switch", "rabbit-test", None),
         ("triage", "port", "rabbit-test2", "et1/1")])


def _shadow_model(ts=None, planned=0):
    return {"ran": ts is not None, "converged": planned == 0,
            "latest_plan_ts": ts, "planned_count": planned,
            "by_owner_action": [{"owner_role": "post", "action": "upsert",
                                 "count": planned}] if planned else [],
            "skipped": {}, "skip_total": 0, "plan_rows": [], "events": [],
            "desired_summary": []}


def test_is_stale_none_and_old_and_fresh():
    from flax_control.shadow_view import is_stale
    assert is_stale(None, _NOW, 180) is True
    old = _NOW - datetime.timedelta(seconds=600)
    assert is_stale(old, _NOW, 180) is True
    fresh = _NOW - datetime.timedelta(seconds=30)
    assert is_stale(fresh, _NOW, 180) is False


def test_parse_role_ui_links():
    from flax_control.lanes_view import parse_role_ui_links
    assert parse_role_ui_links("post=http://p:8446,triage=http://t") == \
        {"post": "http://p:8446", "triage": "http://t"}
    assert parse_role_ui_links("") == {}
    assert parse_role_ui_links("garbage") == {}


def test_build_lanes_one_card_per_role_with_data():
    from flax_control.lanes_view import build_lanes
    fresh = _NOW - datetime.timedelta(seconds=30)
    cards = build_lanes(
        _registry(),
        desired_rows=[("post", "bmc", 44, "t1"), ("post", "host", 44, "t1"),
                      ("triage", "bmc", 6, "t2")],
        shadow_model=_shadow_model(ts=fresh),
        record_rows=[("post", "inventory", 3, 44)],
        roam_rows=[("post", 2, 1)],
        role_ui_links={"post": "http://post-ui"},
        now=_NOW, stale_secs=180)
    by_name = {c["name"]: c for c in cards}
    assert set(by_name) == {"post", "triage"}
    post = by_name["post"]
    assert {d["kind"]: d["count"] for d in post["desired"]} == \
        {"bmc": 44, "host": 44}
    assert post["records"] == [{"kind": "inventory", "n_24h": 3, "n_7d": 44}]
    assert (post["roam_in"], post["roam_out"]) == (2, 1)
    assert post["ui_link"] == "http://post-ui"
    assert post["materializer_stale"] is False
    assert by_name["triage"]["ui_link"] is None


def test_build_lanes_freeze_warning_when_summary_stale():
    from flax_control.lanes_view import build_lanes
    cards = build_lanes(_registry(), [], _shadow_model(ts=None), [], [],
                        {}, _NOW, 180)
    assert all(c["materializer_stale"] for c in cards)


def test_lanes_route_renders(client):
    fresh_iso = "2026-07-05T12:00:00+00:00"
    with patch("flax_control.queries.roles",
               return_value=[("post", {}, 1, "t")]), \
         patch("flax_control.queries.role_universe", return_value=[]), \
         patch("flax_control.queries.desired_by_role_kind", return_value=[]), \
         patch("flax_control.queries.materializer_recent", return_value=[]), \
         patch("flax_control.queries.ownership_events_recent", return_value=[]), \
         patch("flax_control.queries.desired_summary", return_value=[]), \
         patch("flax_control.queries.work_record_counts", return_value=[]), \
         patch("flax_control.queries.roaming_role_counts_24h", return_value=[]):
        resp = client.get("/lanes")
    assert resp.status_code == 200
    assert "post" in resp.text


def test_lanes_route_empty_registry_degrades(client):
    with patch("flax_control.queries.roles", return_value=[]), \
         patch("flax_control.queries.role_universe", return_value=[]), \
         patch("flax_control.queries.desired_by_role_kind", return_value=[]), \
         patch("flax_control.queries.materializer_recent", return_value=[]), \
         patch("flax_control.queries.ownership_events_recent", return_value=[]), \
         patch("flax_control.queries.desired_summary", return_value=[]), \
         patch("flax_control.queries.work_record_counts", return_value=[]), \
         patch("flax_control.queries.roaming_role_counts_24h", return_value=[]):
        resp = client.get("/lanes")
    assert resp.status_code == 200
    assert "no roles published" in resp.text.lower()
