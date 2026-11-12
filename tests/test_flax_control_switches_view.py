from flax_control import switches_view


def test_access_match():
    rows = switches_view.build_port_rows(
        [{"port": "Ethernet6/1", "mask": "access", "access_vid": 31}], {"Ethernet6/1": 31})
    assert rows[0]["vlan_state"] == "match"
    assert rows[0]["access_vid_live"] == 31 and rows[0]["access_vid_desired"] == 31


def test_access_drift():
    rows = switches_view.build_port_rows(
        [{"port": "Ethernet6/1", "mask": "access", "access_vid": 10}], {"Ethernet6/1": 31})
    assert rows[0]["vlan_state"] == "drift"


def test_desired_only_when_no_live_vid():
    rows = switches_view.build_port_rows(
        [{"port": "Ethernet6/1", "mask": "access"}], {"Ethernet6/1": 31})
    assert rows[0]["vlan_state"] == "desired-only"


def test_live_only_when_no_desired():
    rows = switches_view.build_port_rows(
        [{"port": "Ethernet6/1", "mask": "access", "access_vid": 31}], {})
    assert rows[0]["vlan_state"] == "live-only"


def test_trunk_is_labelled_not_drifted():
    rows = switches_view.build_port_rows(
        [{"port": "Ethernet52/1", "mask": "trunk"}], {})
    assert rows[0]["vlan_state"] == "trunk"


def test_no_mask_is_none_state():
    rows = switches_view.build_port_rows([{"port": "Cpu"}], {})
    assert rows[0]["vlan_state"] == "none"


from fastapi.testclient import TestClient
from unittest.mock import patch


def test_switch_overview_shows_vid_drift():
    from flax_control.app import app
    ports = [{"port": "Ethernet6/1", "mask": "access", "access_vid": 10, "macs": []}]
    with patch("flax_control.queries.ports_for_switch", return_value=ports), \
         patch("flax_control.queries.desired_ports_for_switch", return_value={"Ethernet6/1": 31}), \
         patch("flax_control.queries.switches", return_value=[{"switch": "rabbit-gouda", "reachable": True}]):
        resp = TestClient(app).get("/switches/rabbit-gouda")
    assert resp.status_code == 200
    assert b"drift" in resp.content
