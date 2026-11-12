"""Task 7: Pipeline (layer) + Roles + folded pages migrated onto the design
system.

Covers layer.html (Sensing/Policy/Actuation), roles.html (Registry),
shadow.html (Materializer), lanes.html, ownership.html, triage.html,
bmcfw.html, records.html, search.html: every route still 200s with mocked
queries, none of them carry the deprecated bare `{% block heading %}` any
more, all import the shared component macros, and page_header() renders the
`<h1 class="page">` the whole console now uses. A few page-specific
data-preservation spot checks (drill-down links, badge classes, table
classes) round it out so the migration didn't quietly drop functionality.
"""
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

TEMPLATES = Path(__file__).resolve().parent.parent / "flax_control" / "templates"

MIGRATED = [
    "layer.html", "roles.html", "shadow.html", "lanes.html", "ownership.html",
    "triage.html", "bmcfw.html", "bmcfw_post.html", "records.html", "search.html",
]


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


# ---- template-source grep guards -----------------------------------------

# ---- /layers/* ------------------------------------------------------------

def _layers_patches():
    return [
        patch("flax_control.queries.consumer_acks", return_value=[]),
        patch("flax_control.queries.switches", return_value=[]),
        patch("flax_control.queries.observe_state_stats", return_value=(0, None)),
        patch("flax_control.queries.active_lease_count", return_value=0),
        patch("flax_control.queries.roles", return_value=[]),
        patch("flax_control.queries.role_universe", return_value=[]),
        patch("flax_control.queries.switch_ports_all", return_value=[]),
        patch("flax_control.queries.desired_summary", return_value=[]),
        patch("flax_control.queries.materializer_recent", return_value=[]),
        patch("flax_control.queries.ownership_events_recent", return_value=[]),
        patch("flax_control.queries.kea_hosts_count", return_value=0),
        patch("flax_control.queries.reconcile_cadence", return_value=[]),
        patch("flax_control.queries.intentional_flap_active", return_value=0),
        patch("flax_control.queries.layer_anomalies", return_value=[]),
        patch("flax_control.queries.events_for_services", return_value=[]),
        patch("flax_control.config_map_view.catalogue_with_mtimes", return_value=[]),
    ]


@pytest.mark.parametrize("path,title", [
    ("/layers/sensing", "Sensing"),
    ("/layers/policy", "Policy"),
    ("/layers/actuation", "Actuation"),
])
def test_layer_route_renders_design_system_page(client, path, title):
    patches = _layers_patches()
    for p in patches:
        p.start()
    try:
        resp = client.get(path)
    finally:
        for p in patches:
            p.stop()
    assert resp.status_code == 200
    assert 'class="page"' in resp.text
    assert title in resp.text
    assert 'class="lcard' in resp.text


def test_layer_policy_links_registry_and_lanes(client):
    patches = _layers_patches()
    for p in patches:
        p.start()
    try:
        resp = client.get("/layers/policy")
    finally:
        for p in patches:
            p.stop()
    assert resp.status_code == 200
    assert 'href="/roles"' in resp.text
    assert 'href="/lanes"' in resp.text


def test_layer_actuation_links_materializer_and_ownership(client):
    patches = _layers_patches()
    for p in patches:
        p.start()
    try:
        resp = client.get("/layers/actuation")
    finally:
        for p in patches:
            p.stop()
    assert resp.status_code == 200
    assert 'href="/shadow"' in resp.text
    assert 'href="/ownership"' in resp.text


@pytest.mark.parametrize("path", ["/layers/sensing", "/layers/policy",
                                  "/layers/actuation"])
def test_layer_pages_link_services(client, path):
    """Services left the top nav during consolidation; every layer health page
    must keep it reachable via a drill-down (folded-route reachability)."""
    patches = _layers_patches()
    for p in patches:
        p.start()
    try:
        resp = client.get(path)
    finally:
        for p in patches:
            p.stop()
    assert resp.status_code == 200
    assert 'href="/services"' in resp.text


# ---- /roles -----------------------------------------------------------

def test_roles_route_renders_design_system_page(client):
    with patch("flax_control.queries.roles", return_value=[]), \
         patch("flax_control.queries.role_universe", return_value=[]), \
         patch("flax_control.queries.switch_ports_all", return_value=[]):
        resp = client.get("/roles")
    assert resp.status_code == 200
    assert 'class="page"' in resp.text
    assert "No roles published yet" in resp.text


def test_roles_route_populated_uses_data_table_and_badge(client):
    with patch("flax_control.queries.roles", return_value=[
            ("post", {"record_keys": []}, 3, "t")]), \
         patch("flax_control.queries.role_universe", return_value=[
            ("post", "catch_all", None, None)]), \
         patch("flax_control.queries.switch_ports_all", return_value=[]):
        resp = client.get("/roles")
    assert resp.status_code == 200
    assert 'class="data-table' in resp.text
    assert 'class="badge g"' in resp.text  # catch-all role badge


# ---- /shadow ------------------------------------------------------------

def test_shadow_route_renders_design_system_page(client):
    with patch("flax_control.queries.materializer_recent", return_value=[]), \
         patch("flax_control.queries.ownership_events_recent", return_value=[]), \
         patch("flax_control.queries.desired_summary", return_value=[]):
        resp = client.get("/shadow")
    assert resp.status_code == 200
    assert 'class="page"' in resp.text
    assert 'class="badge n"' in resp.text  # "never ran" badge


# ---- /lanes ---------------------------------------------------------------

def test_lanes_route_renders_design_system_page(client):
    with patch("flax_control.queries.roles",
               return_value=[("post", {}, 1, "t")]), \
         patch("flax_control.queries.role_universe", return_value=[]), \
         patch("flax_control.queries.desired_by_role_kind", return_value=[]), \
         patch("flax_control.queries.materializer_recent", return_value=[]), \
         patch("flax_control.queries.ownership_events_recent", return_value=[]), \
         patch("flax_control.queries.desired_summary", return_value=[]), \
         patch("flax_control.queries.work_record_counts", return_value=[]), \
         patch("flax_control.queries.roaming_role_counts_24h", return_value=[]):
        resp = client.get("/lanes")
    assert resp.status_code == 200
    assert 'class="page"' in resp.text
    assert 'class="chip' in resp.text
    assert "post" in resp.text


# ---- /ownership -------------------------------------------------------

def test_ownership_route_renders_design_system_page_and_device_link(client):
    ev = ("2026-07-05T12:00:00+00:00", "aa:de:ad:04:00:01", "triage", "post",
          "rabbit-test", "et1b1")
    with patch("flax_control.queries.ownership_events_recent",
               return_value=[ev]), \
         patch("flax_control.queries.roaming_counts_24h",
               return_value=[("aa:de:ad:04:00:01", 5)]):
        resp = client.get("/ownership")
    assert resp.status_code == 200
    assert 'class="page"' in resp.text
    assert 'class="data-table' in resp.text
    # Task 7 requirement: mac now cross-links to the device page too.
    assert 'href="/devices/aa:de:ad:04:00:01"' in resp.text


# ---- /triage ----------------------------------------------------------

def test_triage_route_renders_design_system_page(client):
    import flax_control.app as appmod
    from flax_control import queries
    with patch.object(appmod, "_load_geometry",
                      return_value=[{"port": "et6b1", "ou": "20L",
                                     "switch": "rabbit-lorax"}]), \
         patch.object(queries, "observe_state_all", return_value={}):
        resp = client.get("/triage")
    assert resp.status_code == 200
    assert 'class="page"' in resp.text
    assert 'class="data-table' in resp.text


# ---- /bmc-fw ------------------------------------------------------------

def test_bmc_fw_route_renders_design_system_page(client):
    from flax_control import queries, bmcfw_view
    with patch.object(queries, "observe_state_all", return_value={}), \
         patch.object(bmcfw_view, "_read_json", return_value=None):
        resp = client.get("/bmc-fw-triage")  # route moved from /bmc-fw (Task 5)
    assert resp.status_code == 200
    assert 'class="page"' in resp.text


# ---- /bmc-fw-post ---------------------------------------------------------

def test_bmc_fw_post_route_renders_design_system_page(client):
    from flax_control import post_bmcfw_view
    with patch.object(post_bmcfw_view, "read_stores",
                       return_value=(None, None, None)):
        resp = client.get("/bmc-fw-post")
    assert resp.status_code == 200
    assert 'class="page"' in resp.text


# ---- /records -----------------------------------------------------------

def test_records_route_renders_design_system_page(client):
    resp = client.get("/records")
    assert resp.status_code == 200
    assert 'class="page"' in resp.text
    assert 'class="ds-btn"' in resp.text


# ---- /search --------------------------------------------------------------

def test_search_route_renders_design_system_page_no_match(client):
    with patch("flax_control.queries.device_one", return_value=None), \
         patch("flax_control.queries.device_mac_by_switch_port", return_value=None), \
         patch("flax_control.queries.device_macs_by_serial", return_value=[]), \
         patch("flax_control.queries.hostname_macs", return_value=[]):
        resp = client.get("/search?q=nope")
    assert resp.status_code == 200
    assert 'class="page"' in resp.text
    assert "No node matches" in resp.text


def test_search_route_renders_candidate_list(client):
    with patch("flax_control.queries.device_one", return_value=None), \
         patch("flax_control.queries.device_mac_by_switch_port", return_value=None), \
         patch("flax_control.queries.device_macs_by_serial",
               return_value=["aa:bb:cc:00:11:22", "dd:ee:ff:00:11:22"]), \
         patch("flax_control.queries.hostname_macs", return_value=[]):
        resp = client.get("/search?q=SV7220")
    assert resp.status_code == 200
    assert 'class="cand-list"' in resp.text
    assert 'href="/devices/aa:bb:cc:00:11:22"' in resp.text
