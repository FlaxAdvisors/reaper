"""Task 3: Nodes + Switches pages migrated onto the design system.

Covers the 4 templates (devices.html, switches.html, switch_overview.html,
switch_detail.html): each route still 200s with mocked queries, rows link
through to the expected drill-down routes, the VLAN drift badge from the
prior switches_view work still renders, and the reconcile flap form is
still present and wired to the same POST target.
"""
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TEMPLATES = Path(__file__).resolve().parent.parent / "flax_control" / "templates"


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


# ---- template-source grep guards -----------------------------------------

# ---- /devices (Nodes) ------------------------------------------------------

def test_devices_page_rows_link_to_device_detail(client, monkeypatch):
    from flax_control import queries
    monkeypatch.setattr(queries, "devices", lambda: [
        {"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-fiesta", "port": "et6b1",
         "kind": "bmc", "family": "tiogapass", "last_seen": "2026-06-17T10:00:00"},
    ])
    r = client.get("/devices")
    assert r.status_code == 200
    assert 'href="/devices/aa:bb:cc:00:00:01"' in r.text
    assert 'class="data-table' in r.text


# ---- /switches --------------------------------------------------------

def test_switches_page_renders_data_table(client, monkeypatch):
    from flax_control import queries
    monkeypatch.setattr(queries, "switches", lambda: [
        {"switch": "rabbit-lorax", "driver": "eos", "polled_at": "2026-06-17T00:00:00",
         "reachable": True, "generation": 5, "port_count": 2},
    ])
    r = client.get("/switches")
    assert r.status_code == 200
    assert 'class="data-table' in r.text
    assert 'href="/switches/rabbit-lorax"' in r.text


# ---- /switches/<sw> overview -------------------------------------------

def test_switch_overview_drift_badge_still_renders(client):
    ports = [{"port": "Ethernet6/1", "mask": "access", "access_vid": 10, "macs": []}]
    with patch("flax_control.queries.ports_for_switch", return_value=ports), \
         patch("flax_control.queries.desired_ports_for_switch", return_value={"Ethernet6/1": 31}), \
         patch("flax_control.queries.switches", return_value=[{"switch": "rabbit-gouda", "reachable": True}]):
        r = client.get("/switches/rabbit-gouda")
    assert r.status_code == 200
    assert 'class="badge w"' in r.text
    assert "drift" in r.text


# ---- /switches/<sw>/<port> detail ---------------------------------------

def test_switch_detail_flap_form_still_present(client):
    sf_ports = [{"port": "Et6/1", "link": "link", "macs": ["aa:bb"], "bmc_mac": "aa:bb",
                 "nic_macs": [], "junk_macs": [], "classification_source": "lldp",
                 "lldp_disagreement": False, "mask": "access"}]
    with patch("flax_control.queries.ports_for_switch", return_value=sf_ports), \
         patch("flax_control.queries.observe_state_one", return_value=None), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_status_for_port", return_value=None):
        r = client.get("/switches/rabbit-lorax/Et6/1")
    assert r.status_code == 200
    body = r.text
    assert 'action="/reconcile/flap"' in body
    assert 'name="switch" value="rabbit-lorax"' in body
    assert 'name="port" value="Et6/1"' in body
    assert 'name="return_to" value="/switches/rabbit-lorax/Et6/1"' in body
    assert 'class="ds-btn"' in body
