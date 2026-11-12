from flax_control import leases_view

RES = [{"mac": "aa:bb:cc:00:11:22", "hostname": "fl001-a3"}]


def test_matched_lease():
    m = leases_view.build_leases(
        [{"mac": "aa:bb:cc:00:11:22", "ip": "10.0.0.5"}], RES)
    assert m["rows"][0]["matched"] is True
    assert m["rows"][0]["reservation"]["hostname"] == "fl001-a3"
    assert m["unreserved_count"] == 0


def test_unreserved_lease_flagged_and_counted():
    m = leases_view.build_leases([{"mac": "de:ad:be:ef:00:01", "ip": "10.0.0.9"}], RES)
    assert m["rows"][0]["matched"] is False
    assert m["unreserved_count"] == 1


def test_unreserved_sorted_first():
    m = leases_view.build_leases([
        {"mac": "aa:bb:cc:00:11:22", "ip": "10.0.0.5"},   # matched
        {"mac": "de:ad:be:ef:00:01", "ip": "10.0.0.9"},   # unreserved
    ], RES)
    assert m["rows"][0]["matched"] is False
    assert m["rows"][1]["matched"] is True


def test_duid_v6_lease_is_unreserved():
    m = leases_view.build_leases([{"mac": "duid:0004aabb", "ip": "fe80::1"}], RES)
    assert m["rows"][0]["matched"] is False
    assert m["unreserved_count"] == 1


def test_match_is_case_insensitive():
    m = leases_view.build_leases([{"mac": "AA:BB:CC:00:11:22", "ip": "10.0.0.5"}], RES)
    assert m["rows"][0]["matched"] is True


from fastapi.testclient import TestClient
from unittest.mock import patch


def test_leases_page_flags_unreserved():
    from flax_control.app import app
    leases = [{"mac": "de:ad:be:ef:00:01", "ip": "10.0.0.9", "subnet_id": 1, "expires": "x"}]
    with patch("flax_control.queries.leases", return_value=leases), \
         patch("flax_control.queries.reservations", return_value=[]):
        resp = TestClient(app).get("/leases")
    assert resp.status_code == 200
    assert b"unreserved" in resp.content
