"""Task 2: breadcrumb trails on the switch + DUT pages.

switch_detail / switch_overview / device_detail should each render a
`.crumbs` breadcrumb nav (via the new `c.breadcrumb(crumbs)` macro in
_components.html) instead of the old "← all switches" back-link.
"""
from fastapi.testclient import TestClient
from unittest.mock import patch


def test_switch_detail_renders_breadcrumb():
    from flax_control.app import app
    sf_ports = [{"port": "Ethernet10/1", "link": "link",
                 "macs": ["aa:bb"], "bmc_mac": "aa:bb",
                 "nic_macs": [], "junk_macs": [],
                 "classification_source": "lldp",
                 "lldp_disagreement": False, "mask": "access"}]
    os_row = {"switch": "rabbit-gouda", "port": "et10b1",
              "vars": {}, "last_polled": "...", "generation": 1}
    status = {"converged": True, "live_mismatches": 0, "macs": []}
    with patch("flax_control.queries.ports_for_switch", return_value=sf_ports), \
         patch("flax_control.queries.observe_state_one", return_value=os_row), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_status_for_port", return_value=status):
        resp = TestClient(app).get("/switches/rabbit-gouda/Ethernet10/1")
    assert resp.status_code == 200
    body = resp.text
    assert 'class="crumbs"' in body
    assert 'href="/switches"' in body
    assert 'href="/switches/rabbit-gouda"' in body
    # current port renders as non-link text within the breadcrumb, not a
    # second /switches/rabbit-gouda/Ethernet10/1 link.
    assert '<a href="/switches/rabbit-gouda/Ethernet10/1">' not in body
    assert '<span class="cur">Ethernet10/1</span>' in body


def test_switch_overview_renders_breadcrumb():
    from flax_control.app import app
    ports = [{"port": "Ethernet10/1", "mask": "access", "access_vid": 31, "macs": []}]
    with patch("flax_control.queries.ports_for_switch", return_value=ports), \
         patch("flax_control.queries.desired_ports_for_switch", return_value={}), \
         patch("flax_control.queries.switches",
               return_value=[{"switch": "rabbit-gouda", "reachable": True}]):
        resp = TestClient(app).get("/switches/rabbit-gouda")
    assert resp.status_code == 200
    body = resp.text
    assert 'class="crumbs"' in body
    assert 'href="/switches"' in body
    assert '<span class="cur">rabbit-gouda</span>' in body


def test_device_detail_renders_breadcrumb():
    from flax_control.app import app
    mac = "aa:bb:cc:00:11:22"
    with patch("flax_control.queries.device_full", return_value=None), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None), \
         patch("flax_control.queries.ownership_events_recent", return_value=[]), \
         patch("flax_control.records_view.lookup_duts", return_value=[]):
        resp = TestClient(app).get(f"/devices/{mac}")
    assert resp.status_code == 200
    body = resp.text
    assert 'class="crumbs"' in body
    assert 'href="/devices"' in body
    assert f'<span class="cur">{mac}</span>' in body
