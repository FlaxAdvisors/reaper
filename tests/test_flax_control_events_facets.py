"""Task-3: /events service/kind/switch filters become value-scoped dropdowns.

`queries.events_facets()` returns the distinct service/kind/switch values
present in audit.events, and the /events template renders them as <select>
options instead of free-text <input> fields, with the currently-applied
filter value marked `selected`.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


FACETS = {
    "services": ["flax-observe", "flax-classify"],
    "kinds": ["bmc_pinged"],
    "switches": ["rabbit-gouda"],
}


def test_events_page_renders_value_scoped_dropdowns(client):
    with patch("flax_control.queries.events_facets", return_value=FACETS), \
         patch("flax_control.queries.events", return_value=[]):
        resp = client.get("/events?service=flax-observe")
    assert resp.status_code == 200
    body = resp.text

    assert '<select name="service">' in body
    assert '<option value="">(any)</option>' in body
    assert '<option value="flax-observe" selected>' in body

    assert '<select name="kind">' in body
    assert '>bmc_pinged</option>' in body

    assert '<select name="switch">' in body
    assert '>rabbit-gouda</option>' in body
