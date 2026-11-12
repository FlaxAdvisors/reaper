"""Phase-5 ownership/roaming ledger: pure shaping + route."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


_EV = ("2026-07-05T12:00:00+00:00", "aa:de:ad:04:00:01", "triage", "post",
       "rabbit-test", "et1b1")


def test_build_ownership_flags_rapid_roamers():
    from flax_control.ownership_view import build_ownership
    counts = [("aa:de:ad:04:00:01", 5), ("aa:de:ad:04:00:02", 1)]
    model = build_ownership([_EV], counts, warn_threshold=4)
    assert model["rapid"] == [{"mac": "aa:de:ad:04:00:01", "count": 5}]
    assert model["events"][0]["mac"] == "aa:de:ad:04:00:01"
    assert model["events"][0]["from_role"] == "triage"
    assert model["warn_threshold"] == 4


def test_build_ownership_empty_is_calm():
    from flax_control.ownership_view import build_ownership
    model = build_ownership([], [], warn_threshold=4)
    assert model["rapid"] == [] and model["events"] == []


def test_ownership_route_renders(client):
    with patch("flax_control.queries.ownership_events_recent",
               return_value=[_EV]) as ev, \
         patch("flax_control.queries.roaming_counts_24h",
               return_value=[("aa:de:ad:04:00:01", 5)]):
        resp = client.get("/ownership")
    assert resp.status_code == 200
    assert "rapid" in resp.text.lower()
    ev.assert_called_once_with(limit=500, mac=None)


def test_ownership_route_mac_filter_forwarded(client):
    with patch("flax_control.queries.ownership_events_recent",
               return_value=[]) as ev, \
         patch("flax_control.queries.roaming_counts_24h", return_value=[]):
        resp = client.get("/ownership?mac=aa:de:ad:04:00:01")
    assert resp.status_code == 200
    ev.assert_called_once_with(limit=500, mac="aa:de:ad:04:00:01")
