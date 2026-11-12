"""Task 4: Reservations + Leases pages migrated onto the design system.

Covers reservations.html + leases.html: each route still 200s with mocked
queries, the operator-note/aliases/delete forms are still present and wired
to their existing POST targets, the mac cell still cross-links to
/devices/<mac>, and the leases unreserved-count badge + per-row
`.lease-unreserved` highlight (shipped earlier) still render.
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

# ---- /reservations ------------------------------------------------------

def test_reservations_page_renders_data_table_and_device_link(client, monkeypatch):
    from flax_control import queries
    monkeypatch.setattr(queries, "reservations", lambda: [{
        "mac": "1c:34:da:7f:b3:a4", "mac_hex": "1c34da7fb3a4",
        "switch": "rabbit-lorax", "port": "et6b1", "kind": "bmc", "vid": 17,
        "ipv4_address": "172.17.6.101", "ipv6_address": None,
        "hostname": "leopard6b1-bmc", "subnet_id": 17,
        "operator_note": None, "aliases": None, "generation": 3,
        "updated_at": "2026-05-24T18:00:00+00:00",
    }])
    r = client.get("/reservations")
    assert r.status_code == 200
    body = r.text
    assert 'class="data-table' in body
    assert 'href="/devices/1c:34:da:7f:b3:a4"' in body
    # operator-note / aliases / delete forms unchanged
    assert 'action="/reservations/1c34da7fb3a4/operator_note"' in body
    assert 'action="/reservations/1c34da7fb3a4/aliases"' in body
    assert 'action="/reservations/1c34da7fb3a4/delete"' in body
    assert 'class="ds-btn ghost"' in body


# ---- /leases --------------------------------------------------------------

def test_leases_page_unreserved_badge_and_row_highlight(client):
    rows = [
        {"mac": "aa:bb:cc:00:00:01", "ip": "172.17.6.2", "subnet_id": 17,
         "expires": "2026-06-17T12:00:00", "state": 0},   # unreserved
    ]
    with patch("flax_control.queries.leases", return_value=rows), \
         patch("flax_control.queries.reservations", return_value=[]):
        r = client.get("/leases")
    assert r.status_code == 200
    body = r.text
    assert 'class="data-table' in body
    assert 'href="/devices/aa:bb:cc:00:00:01"' in body
    assert 'class="lease-unreserved"' in body
    assert 'unreserved · dhcp_release?' in body
    assert 'class="badge w"' in body
