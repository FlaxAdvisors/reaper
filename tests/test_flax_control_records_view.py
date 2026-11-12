"""Route tests for the phase-4 work-record retrieval API (queries patched;
the SQL itself is exercised on gouda via tests/test_flax_post_work_records_db.py)."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


_DUT = {"dut_id": 7, "p0_mac": "aa:de:ad:04:00:01", "serial": "TESTSN-1",
        "first_seen": "2026-07-05T10:00:00+00:00",
        "last_record_at": "2026-07-05T11:00:00+00:00"}
_REC = {"id": 1, "owner_role": "post", "kind": "inventory", "stage": None,
        "keys": {"order": "TESTORD"}, "payload": {"hash": "h"},
        "at": "2026-07-05T11:00:00+00:00"}


def test_duts_lookup_requires_mac_or_serial(client):
    assert client.get("/api/records/duts").status_code == 422


def test_duts_lookup_passes_assembly_default_current(client):
    with patch("flax_control.records_view.lookup_duts",
               return_value=[_DUT]) as m:
        resp = client.get("/api/records/duts?mac=aa:de:ad:04:00:01")
    assert resp.status_code == 200
    assert resp.json() == [_DUT]
    m.assert_called_once_with(mac="aa:de:ad:04:00:01", serial=None,
                              assembly="current")


def test_duts_lookup_assembly_all_forwarded(client):
    with patch("flax_control.records_view.lookup_duts", return_value=[]) as m:
        client.get("/api/records/duts?serial=TESTSN-1&assembly=all")
    m.assert_called_once_with(mac=None, serial="TESTSN-1", assembly="all")


def test_dut_records_route_is_int_typed(client):
    with patch("flax_control.records_view.dut_records",
               return_value=[_REC]) as m:
        resp = client.get("/api/records/7?kind=inventory&limit=5")
    assert resp.status_code == 200
    assert resp.json() == [_REC]
    m.assert_called_once_with(7, kind="inventory", stage=None, since=None,
                              limit=5)
    # a non-int path segment other than the literal "duts" is rejected
    assert client.get("/api/records/notanid").status_code == 422


def test_records_by_order_and_customer(client):
    with patch("flax_control.records_view.records_by_key",
               return_value=[_REC]) as m:
        resp = client.get("/api/records?order=TESTORD")
    assert resp.status_code == 200
    m.assert_called_once_with("order", "TESTORD", limit=200)
    with patch("flax_control.records_view.records_by_key",
               return_value=[]) as m2:
        client.get("/api/records?customer=acme&limit=10")
    m2.assert_called_once_with("customer", "acme", limit=10)


def test_records_requires_order_or_customer(client):
    assert client.get("/api/records").status_code == 422


def test_lookup_duts_current_truncates_to_one():
    """Pure behavior of the assembly=current default (no DB: rows injected)."""
    from flax_control import records_view
    rows = [(7, "aa:de:ad:04:00:01", "NEW", None, None),
            (3, "aa:de:ad:04:00:01", "OLD", None, None)]
    out = records_view._rows_to_duts(rows)
    assert [d["dut_id"] for d in out] == [7, 3]
    assert records_view._apply_assembly(out, "current") == out[:1]
    assert records_view._apply_assembly(out, "all") == out


@pytest.mark.parametrize("raw", [
    "aa:de:ad:04:00:01",
    "AA-DE-AD-04-00-01",
    "aadead040001",
    "aa.de.ad.04.00.01",
])
def test_norm_mac_accepts_writer_forms(raw):
    from flax_control.records_view import _norm_mac
    assert _norm_mac(raw) == "aa:de:ad:04:00:01"


@pytest.mark.parametrize("raw", ["not-a-mac", "", None])
def test_norm_mac_rejects_non_macs(raw):
    from flax_control.records_view import _norm_mac
    assert _norm_mac(raw) is None


def test_records_by_key_rejects_non_whitelisted_key():
    from flax_control import records_view
    with pytest.raises(ValueError):
        records_view.records_by_key("not_a_key", "X")


def test_records_by_key_interpolates_literal_expression_index():
    """The SQL must carry the literal `keys->>'order'` expression (to match
    migration 031's expression index) rather than a parameterized
    `keys->>%s` (which the index can't match, forcing a seq-scan)."""
    from flax_control import records_view

    captured = {}

    class _FakeConn:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return self

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakePool:
        def connection(self):
            return _FakeConn()

    with patch("flax_control.records_view.get_pool", return_value=_FakePool()):
        records_view.records_by_key("order", "X")

    assert "keys->>'order'" in captured["sql"]
    assert "keys->>%s" not in captured["sql"]


def test_lookup_duts_unnormalizable_mac_returns_empty_without_db():
    """An unnormalizable mac must short-circuit before any pool/connection
    access — even with a serial also supplied (constraints are ANDed)."""
    from flax_control import records_view

    def _boom():
        raise AssertionError("get_pool should not be called for a mac that "
                              "cannot normalize")

    with patch("flax_control.records_view.get_pool", side_effect=_boom):
        assert records_view.lookup_duts(mac="junk-mac") == []
        assert records_view.lookup_duts(mac="junk-mac", serial="X") == []
