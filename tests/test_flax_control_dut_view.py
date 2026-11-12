from flax_control import dut_view


def test_biography_merges_and_sorts_newest_first():
    bio = dut_view.build_biography(
        first_seen="2026-07-09T08:00:00Z",
        observe={"resolved": {"chassis_sn": "SN1"}, "last_polled": "2026-07-09T14:00:00Z"},
        reconcile_actions=[{"ts":"2026-07-09T12:00:00Z","action":"set_vid","detail":{"vid":31}}],
        ownership_events=[("2026-07-09T13:00:00Z","aa","triage","post","s","p")],
        work_records=[{"at":"2026-07-09T09:00:00Z","stage":"fw-nic","kind":"fw","owner_role":"post","payload":{}}],
    )
    whens = [i["when"] for i in bio]
    assert whens == sorted(whens, reverse=True)
    sources = {i["source"] for i in bio}
    assert sources == {"device","observe","reconcile","ownership","record"}

def test_ownership_item_names_the_handoff():
    bio = dut_view.build_biography(
        first_seen=None, observe=None, reconcile_actions=[],
        ownership_events=[("2026-07-09T13:00:00Z","aa","triage","post","s","p")],
        work_records=[])
    item = [i for i in bio if i["source"]=="ownership"][0]
    assert "triage" in item["what"] and "post" in item["what"]

def test_none_timestamps_sort_last():
    bio = dut_view.build_biography(
        first_seen=None, observe=None,
        reconcile_actions=[{"ts":None,"action":"x","detail":{}}],
        ownership_events=[], work_records=[{"at":"2026-07-09T10:00:00Z","stage":"s","kind":"k","owner_role":"post","payload":{}}])
    assert bio[0]["when"] == "2026-07-09T10:00:00Z"
    assert bio[-1]["when"] is None


from fastapi.testclient import TestClient
from unittest.mock import patch

def test_device_detail_renders_biography():
    from flax_control.app import app
    full = {"mac":"aa:bb:cc:00:11:22","switch":"s","port":"et6b1","kind":"bmc",
            "latched":{}, "last_seen":"2026-07-09T08:00:00Z",
            "observe":{"resolved":{"chassis_sn":"SN1"},"last_polled":"2026-07-09T14:00:00Z","vars":{}},
            "reservation":None,"desired":None}
    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None), \
         patch("flax_control.queries.ownership_events_recent", return_value=[]), \
         patch("flax_control.records_view.lookup_duts", return_value=[]):
        resp = TestClient(app).get("/devices/aa:bb:cc:00:11:22")
    assert resp.status_code == 200
    assert b"Biography" in resp.content
    assert b"SN1 resolved" in resp.content


def test_datetime_when_from_ownership_does_not_break_sort():
    """Regression: mac_ownership_events.at arrives as a raw datetime (not
    iso'd), while other sources are ISO strings. build_biography must not
    raise TypeError comparing datetime vs str during the newest-first sort.
    Reproduces the live /devices/<mac> 500 seen on gouda (0.9.113)."""
    import datetime
    dt = datetime.datetime(2026, 7, 9, 13, 0, 0, tzinfo=datetime.timezone.utc)
    bio = dut_view.build_biography(
        first_seen="2026-07-09T08:00:00Z",
        observe=None,
        reconcile_actions=[{"ts": "2026-07-09T12:00:00Z", "action": "x", "detail": {}}],
        ownership_events=[(dt, "aa", "triage", "post", "s", "p")],  # raw datetime at
        work_records=[],
    )
    # Must not raise; every when normalised to a string; newest (13:00) first.
    assert all(i["when"] is None or isinstance(i["when"], str) for i in bio)
    assert bio[0]["source"] == "ownership"


def test_device_detail_two_column_layout_with_scrollable_biography():
    """Task 5: /devices/<mac> renders the mockup's DUT layout — a `.dut-cols`
    two-column grid whose LEFT column wraps the biography `.tl` timeline in
    its own `.scroll-panel` (so a long biography doesn't push the right rail
    down) and whose RIGHT column has a pinned "Current status" side-card."""
    from flax_control.app import app
    full = {
        "mac": "aa:bb:cc:00:33:44", "switch": "rabbit-x", "port": "et6b1",
        "kind": "bmc", "latched": {"family": "tiogapass"},
        "last_seen": "2026-07-09T08:00:00Z",
        "reservation": {"ipv4_address": "10.31.0.44", "hostname": "fl001-a3",
                        "subnet_id": 31, "classify": {"vid": 31},
                        "operator_note": None},
        "observe": {"resolved": {"chassis_sn": "SV7220G3-4471"},
                    "last_polled": "2026-07-09T14:00:00Z",
                    "vars": {"linkstate": {"value": "link", "since": "t1"},
                             "bmcpower": {"value": "on", "since": "t2"}}},
        "desired": {"desired_vid": 31, "occupants": [], "generation": 1},
        "port_facts": None, "last_reconcile_action": None, "recent_events": [],
    }
    own_events = [("2026-07-09T13:00:00Z", "aa:bb:cc:00:33:44", "triage", "post", "s", "p")]
    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None), \
         patch("flax_control.queries.ownership_events_recent", return_value=own_events), \
         patch("flax_control.records_view.lookup_duts", return_value=[]):
        resp = TestClient(app).get("/devices/aa:bb:cc:00:33:44")
    assert resp.status_code == 200
    body = resp.text
    # Biography has multiple sources including an ownership handoff item.
    assert "Ownership" in body and "triage" in body and "post" in body
    # The biography timeline sits inside a dedicated scroll panel.
    assert 'class="scroll-panel"' in body
    assert body.index('class="scroll-panel"') < body.index('class="tl"')
    # Two-column DUT body grid is present.
    assert 'class="dut-cols"' in body
    # Right-rail "Current status" card is present (pinned, not pushed down
    # by the biography since that lives in its own scroll panel).
    assert "Current status" in body


def test_device_detail_unknown_mac_still_200_with_dut_head():
    """An unknown MAC (device_full returns None) must still render 200 with
    a graceful 'no device record' note in the identity header."""
    from flax_control.app import app
    with patch("flax_control.queries.device_full", return_value=None), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None), \
         patch("flax_control.queries.ownership_events_recent", return_value=[]), \
         patch("flax_control.records_view.lookup_duts", return_value=[]):
        resp = TestClient(app).get("/devices/ff:ff:ff:ff:ff:ff")
    assert resp.status_code == 200
    body = resp.text
    assert "ff:ff:ff:ff:ff:ff" in body
    assert "No device record found" in body
    # No two-column body for an unknown device (nothing to show).
    assert 'class="dut-cols"' not in body
