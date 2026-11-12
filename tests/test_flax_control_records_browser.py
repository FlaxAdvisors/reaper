"""Phase-5 records browser: detection, pure shaping, route."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


# ── detection (records_view) ────────────────────────────────────────────────

def test_detect_term_mac_forms():
    from flax_control.records_view import detect_term
    for raw in ("AA:DE:AD:04:00:01", "aa-de-ad-04-00-01", "aadead040001"):
        assert detect_term(raw) == ("mac", "aa:de:ad:04:00:01")


def test_detect_term_dut_id_and_text():
    from flax_control.records_view import detect_term
    assert detect_term("42") == ("dut_id", 42)
    assert detect_term("WTH491259HM1A") == ("text", "WTH491259HM1A")
    assert detect_term("  FLAX-001 ") == ("text", "FLAX-001")


# ── pure shaping (records_browser_view) ─────────────────────────────────────

_DUT = {"dut_id": 7, "p0_mac": "aa:de:ad:04:00:01", "serial": "TESTSN-1",
        "first_seen": "t0", "last_record_at": "t1"}
_REC = {"id": 3, "owner_role": "post", "kind": "fw-flash", "stage": "Firmware",
        "keys": {"order": "TESTORD"},
        "payload": {"terminal": "done", "current": "1.0.8", "target": "1.0.8",
                    "fault_reason": "", "port": "et1b1", "timeline": []},
        "at": "t1"}


def test_build_search_groups_hits_and_tried():
    from flax_control.records_browser_view import build_search
    model = build_search("term", mac_duts=[], id_dut=None, serial_duts=[_DUT],
                         order_duts=[], customer_duts=[])
    assert model["hits"]["duts"] == [_DUT]
    assert model["hits"]["orders"] == [] and model["hits"]["customers"] == []
    assert "serial" in model["tried"]
    assert model["any"] is True


def test_build_search_empty_says_tried():
    from flax_control.records_browser_view import build_search
    model = build_search("nope", mac_duts=[], id_dut=None, serial_duts=[],
                         order_duts=[], customer_duts=[])
    assert model["any"] is False
    assert set(model["tried"]) >= {"serial", "order", "customer"}


def test_record_summary_fw_flash_and_fallback():
    from flax_control.records_browser_view import record_summary
    s = record_summary("fw-flash", _REC["payload"])
    assert "done" in s and "1.0.8" in s
    assert record_summary("inventory", {"hash": "abc", "fru": {}}) != ""


def test_build_biography_identity_and_assemblies():
    from flax_control.records_browser_view import build_biography
    older = dict(_DUT, dut_id=3, serial="", last_record_at="t-1")
    model = build_biography(_DUT, assemblies=[_DUT, older], records=[_REC])
    assert model["dut"]["dut_id"] == 7
    assert model["serial_unreadable"] is False
    assert [a["dut_id"] for a in model["other_assemblies"]] == [3]
    assert model["records"][0]["summary"]
    assert model["kinds"] == ["fw-flash"]


def test_build_biography_unreadable_serial_badge():
    from flax_control.records_browser_view import build_biography
    d = dict(_DUT, serial="")
    model = build_biography(d, assemblies=[d], records=[])
    assert model["serial_unreadable"] is True


# ── route ───────────────────────────────────────────────────────────────────

def test_records_page_renders_search(client):
    with patch("flax_control.records_view.lookup_duts", return_value=[_DUT]), \
         patch("flax_control.records_view.records_by_key", return_value=[]), \
         patch("flax_control.records_view.duts_for_key", return_value=[]):
        resp = client.get("/records?q=aa:de:ad:04:00:01")
    assert resp.status_code == 200
    assert "aa:de:ad:04:00:01" in resp.text


def test_records_page_biography(client):
    with patch("flax_control.records_view.dut_by_id", return_value=_DUT), \
         patch("flax_control.records_view.lookup_duts", return_value=[_DUT]), \
         patch("flax_control.records_view.dut_records", return_value=[_REC]):
        resp = client.get("/records?dut=7")
    assert resp.status_code == 200
    assert "TESTSN-1" in resp.text and "fw-flash" in resp.text


def test_records_page_order_slice(client):
    row = dict(_REC, dut_id=7, p0_mac="aa:de:ad:04:00:01", serial="TESTSN-1")
    with patch("flax_control.records_view.records_by_key",
               return_value=[row]) as m:
        resp = client.get("/records?order=TESTORD")
    assert resp.status_code == 200
    m.assert_called_once_with("order", "TESTORD", limit=500)


def test_records_page_empty_landing(client):
    resp = client.get("/records")
    assert resp.status_code == 200          # no q -> search landing, no DB hit
