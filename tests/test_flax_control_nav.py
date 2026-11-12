"""Task-1 nav contract: the INSPECT IA.

Phase-5 grew the sidebar to two axes (layers + role lanes) stacked on top of
every pre-existing route, producing a 17-link nav. Task-2 consolidated that
down to 12 links across OVERVIEW/PIPELINE/ROLES. This is the follow-on INSPECT
restructure: /lanes moves from the ROLES group into PIPELINE (it is a
pipeline-lane view, not a role-specific one), the ROLES group is renamed
INSPECT and gains /events and /services (both promoted out of the folded
set), for 14 top-level links across three groups (OVERVIEW/PIPELINE/INSPECT).
The routes that remain folded out of the sidebar (Registry /roles,
Materializer /shadow, /ownership, /bmc-fw) are NOT deleted -- they stay live
(200) for bookmarks/external consumers and get drill-down links from other
pages in a later task. This file therefore asserts two separate things
instead of one "every href is in the nav" contract: (1) the top-level nav
matches the new IA exactly, and (2) the folded routes are still reachable
even though they left the nav.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


# The approved INSPECT IA, in group order.
OVERVIEW_HREFS = (
    'href="/"', 'href="/devices"', 'href="/switches"',
    'href="/reservations"', 'href="/leases"', 'href="/config"',
)
PIPELINE_HREFS = (
    'href="/layers/sensing"', 'href="/layers/policy"',
    'href="/layers/actuation"', 'href="/lanes"',
)
INSPECT_HREFS = (
    'href="/triage"', 'href="/records"', 'href="/events"', 'href="/services"',
)
TOP_LEVEL_HREFS = OVERVIEW_HREFS + PIPELINE_HREFS + INSPECT_HREFS

# Routes folded OUT of the top-level nav. Still live, just not in <nav>.
FOLDED_HREFS = (
    'href="/roles"', 'href="/shadow"', 'href="/ownership"',
    'href="/bmc-fw"',
)


def _rendered_nav(client):
    """Render any page through base.html -- /devices is the cheapest one to
    mock and is used elsewhere in the suite for the same purpose."""
    from flax_control import queries
    with patch.object(queries, "devices", return_value=[]):
        resp = client.get("/devices")
    assert resp.status_code == 200
    return resp.text


def test_top_level_nav_matches_consolidated_ia(client):
    html = _rendered_nav(client)
    for href in TOP_LEVEL_HREFS:
        assert href in html, f"consolidated-IA nav link missing: {href}"


def test_nav_groups_present(client):
    html = _rendered_nav(client).replace("\n", "")
    for label in ("OVERVIEW", "PIPELINE", "INSPECT"):
        assert label in html, f"nav group missing: {label}"


def test_lanes_labelled_lanes_not_post(client):
    """/lanes moved from ROLES to PIPELINE and dropped its old 'Post' label
    -- it must render as the generic 'Lanes' link text."""
    html = _rendered_nav(client)
    assert ">Lanes<" in html
    assert ">Post<" not in html


def test_folded_routes_not_in_top_level_nav(client):
    """Registry/Materializer/Ownership/BMC-FW left the sidebar -- their
    hrefs must not appear as nav links any more."""
    html = _rendered_nav(client)
    for href in FOLDED_HREFS:
        assert href not in html, f"folded route still in top-level nav: {href}"


def test_omnibox_present(client):
    html = _rendered_nav(client)
    assert 'action="/search"' in html
    assert 'name="q"' in html


def test_htmx_vendored_not_cdn(client):
    html = _rendered_nav(client)
    assert '/static/htmx.min.js' in html
    assert 'unpkg.com' not in html and 'cdn' not in html.lower()


# ---------------------------------------------------------------------------
# Frozen-route guarantee: folded routes left the nav but must stay live.
# ---------------------------------------------------------------------------

def test_roles_route_still_live(client):
    from flax_control import queries
    with patch.object(queries, "roles", return_value=[]), \
         patch.object(queries, "role_universe", return_value=[]), \
         patch.object(queries, "switch_ports_all", return_value=[]):
        resp = client.get("/roles")
    assert resp.status_code == 200


def test_shadow_route_still_live(client):
    from flax_control import queries
    with patch.object(queries, "materializer_recent", return_value=[]), \
         patch.object(queries, "ownership_events_recent", return_value=[]), \
         patch.object(queries, "desired_summary", return_value=[]):
        resp = client.get("/shadow")
    assert resp.status_code == 200


def test_ownership_route_still_live(client):
    from flax_control import queries
    with patch.object(queries, "ownership_events_recent", return_value=[]), \
         patch.object(queries, "roaming_counts_24h", return_value=[]):
        resp = client.get("/ownership")
    assert resp.status_code == 200


def test_bmc_fw_route_still_live(client):
    """Task 5: /bmc-fw is now a 303 redirect to /bmc-fw-triage — still live
    (not 404) for bookmarks/external consumers, just no longer the page
    itself."""
    resp = client.get("/bmc-fw", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/bmc-fw-triage"


def test_bmc_fw_triage_route_still_live(client):
    from flax_control import queries, bmcfw_view
    with patch.object(queries, "observe_state_all", return_value={}), \
         patch.object(bmcfw_view, "_read_json", return_value=None):
        resp = client.get("/bmc-fw-triage")
    assert resp.status_code == 200


def test_folded_routes_still_registered_in_app(client):
    """Belt-and-braces: the route table itself still carries every folded
    path, independent of how heavy each view's query mocking is."""
    from flax_control.app import app
    registered = {getattr(r, "path", None) for r in app.routes}
    for path in ("/roles", "/shadow", "/ownership", "/bmc-fw", "/bmc-fw-triage"):
        assert path in registered, f"folded route de-registered: {path}"


# ---------------------------------------------------------------------------
# INSPECT-promoted routes: /events and /services are top-level nav links now
# (Task-1), not folded -- keep their liveness checks alongside the rest of
# the INSPECT group.
# ---------------------------------------------------------------------------

def test_events_route_still_live(client):
    from flax_control import queries
    with patch.object(queries, "events", return_value=[]), \
         patch.object(queries, "events_facets",
                       return_value={"services": [], "kinds": [], "switches": []}):
        resp = client.get("/events")
    assert resp.status_code == 200


def test_services_route_still_live(client):
    from flax_control import queries
    with patch.object(queries, "consumer_acks", return_value=[]):
        resp = client.get("/services")
    assert resp.status_code == 200
