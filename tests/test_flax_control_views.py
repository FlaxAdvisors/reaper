"""Tests for flax_control JSON API + HTML view endpoints."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


def test_api_switches_returns_list(client):
    rows = [{"switch": "rabbit-lorax", "driver": "eos",
              "polled_at": "2026-05-24T13:00:00Z",
              "reachable": True, "generation": 412, "port_count": 80}]
    with patch("flax_control.queries.switches", return_value=rows):
        resp = client.get("/api/v1/switches")
    assert resp.status_code == 200
    assert resp.json() == rows


def test_api_switch_ports_returns_per_port(client):
    rows = [{"port": "Ethernet6/1", "link": "link",
              "macs": ["aa"], "bmc_mac": "aa", "nic_macs": [],
              "junk_macs": [], "classification_source": "lldp",
              "lldp_disagreement": False, "mask": "access"}]
    with patch("flax_control.queries.ports_for_switch", return_value=rows):
        resp = client.get("/api/v1/switches/rabbit-lorax/ports")
    assert resp.status_code == 200
    assert resp.json() == rows


def test_api_observe_state_returns_keyed_dict(client):
    state = {"rabbit-lorax:et6b1": {
        "switch": "rabbit-lorax", "port": "et6b1",
        "vars": {"linkstate": {"value": "link", "since": "..."}},
        "last_polled": "2026-05-24T13:00:00Z", "generation": 53,
    }}
    with patch("flax_control.queries.observe_state_all", return_value=state):
        resp = client.get("/api/v1/observe/state")
    assert resp.status_code == 200
    body = resp.json()
    assert "rabbit-lorax:et6b1" in body
    assert body["rabbit-lorax:et6b1"]["vars"]["linkstate"]["value"] == "link"


def test_api_observe_state_one_returns_row_or_404(client):
    one = {"switch": "rabbit-lorax", "port": "et6b1",
            "vars": {"linkstate": {"value": "link", "since": "..."}},
            "last_polled": "2026-05-24T13:00:00Z", "generation": 53}
    with patch("flax_control.queries.observe_state_one", return_value=one):
        resp = client.get("/api/v1/observe/state/rabbit-lorax/et6b1")
    assert resp.status_code == 200
    assert resp.json()["switch"] == "rabbit-lorax"

    with patch("flax_control.queries.observe_state_one", return_value=None):
        resp = client.get("/api/v1/observe/state/rabbit-lorax/et99/0")
    assert resp.status_code == 404


def test_api_events_returns_filtered_rows(client):
    rows = [{"id": 1, "ts": "...", "service": "flax-observe",
              "kind": "transition", "mac": None, "switch": "rabbit-lorax",
              "port": "et6b1", "payload": {"var": "linkstate"}}]
    with patch("flax_control.queries.events", return_value=rows) as m:
        resp = client.get("/api/v1/events?service=flax-observe&limit=50")
    assert resp.status_code == 200
    assert resp.json() == rows
    # Confirm query was called with the filter
    _, kwargs = m.call_args
    assert kwargs.get("service") == "flax-observe"
    assert kwargs.get("limit") == 50


def test_api_acks_returns_rows(client):
    rows = [{"consumer": "flax-observe", "source": "switch_facts",
              "generation": 412, "action": "applied",
              "consumed_at": "...", "detail": None}]
    with patch("flax_control.queries.consumer_acks", return_value=rows):
        resp = client.get("/api/v1/acks")
    assert resp.status_code == 200
    assert resp.json() == rows


def test_api_ports_returns_list_of_port_tokens(client):
    """/api/v1/ports returns [port_token, ...] matching switchportrecond shape."""
    state = {
        "rabbit-lorax:et6b1": {"switch": "rabbit-lorax", "port": "et6b1"},
        "rabbit-lorax:et6b2": {"switch": "rabbit-lorax", "port": "et6b2"},
    }
    geometry = [{"port": "et6b1", "ou": "20L"}, {"port": "et6b2", "ou": "22L"}]
    with patch("flax_control.queries.observe_state_all", return_value=state), \
         patch("flax_control.app._load_geometry", return_value=geometry):
        resp = client.get("/api/v1/ports")
    assert resp.status_code == 200
    body = resp.json()
    assert body == ["et6b1", "et6b2"]


def test_api_ports_excludes_turtle_swp_ports(client):
    """Turtle (Cumulus OOB-mgmt) ports are NOT in the rack geometry, so the
    switchportrecond-compat /api/v1/ports must exclude them -- otherwise the
    legacy triage UI parseInt()s their empty ou and renders NaN rows."""
    state = {
        "rabbit-lorax:et6b1": {"switch": "rabbit-lorax", "port": "et6b1"},
        "turtle-lorax:swp44": {"switch": "turtle-lorax", "port": "swp44"},
        "turtle-lorax:swp23": {"switch": "turtle-lorax", "port": "swp23"},
    }
    geometry = [{"port": "et6b1", "ou": "20L"}]   # turtle ports absent
    with patch("flax_control.queries.observe_state_all", return_value=state), \
         patch("flax_control.app._load_geometry", return_value=geometry):
        resp = client.get("/api/v1/ports")
    assert resp.status_code == 200
    assert resp.json() == ["et6b1"]   # swp44/swp23 excluded


def test_api_port_404_for_turtle_swp_port(client):
    """/api/v1/port/<turtle swp> 404s -- not part of the triage rack view."""
    state = {"turtle-lorax:swp44": {"switch": "turtle-lorax", "port": "swp44"}}
    geometry = [{"port": "et6b1", "ou": "20L"}]   # swp44 absent from rack geometry
    with patch("flax_control.queries.observe_state_all", return_value=state), \
         patch("flax_control.app._load_geometry", return_value=geometry):
        resp = client.get("/api/v1/port/swp44")
    assert resp.status_code == 404


def test_api_port_returns_triage_status(client, monkeypatch):
    """/api/v1/port/<token> returns switchportrecond-shaped status.json."""
    state = {"rabbit-lorax:et6b1": {
        "switch": "rabbit-lorax", "port": "et6b1",
        "vars": {"linkstate": {"value": "link", "since": "t1"}},
        "last_polled": "...", "generation": 1,
    }}
    geometry = [{"port": "et6b1", "ou": "20L", "switch": "rabbit-lorax"}]
    with patch("flax_control.queries.observe_state_all", return_value=state), \
         patch("flax_control.app._load_geometry", return_value=geometry):
        resp = client.get("/api/v1/port/et6b1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["port"] == "Et6/1"        # display form
    assert body["state"] == "linkstate"


def test_api_port_404_when_missing(client):
    with patch("flax_control.queries.observe_state_all", return_value={}):
        resp = client.get("/api/v1/port/et99b1")
    assert resp.status_code == 404


def test_api_rackous_returns_geometry_and_rackname(client):
    geometry = [{"port": "et6b1", "ou": "20L", "switch": "rabbit-lorax"},
                {"port": "et6b2", "ou": "22L", "switch": "rabbit-lorax"}]
    with patch("flax_control.app._load_geometry", return_value=geometry), \
         patch("flax_control.app._read_rackname", return_value="bang-fiesta"):
        resp = client.get("/api/v1/rackous")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rackName"] == "bang-fiesta"
    assert body["maxPower"] == 0   # switchportrecond returns 0 here today
    assert {"ou": "20", "port": "Et6/1"} in body["geometry"]


def test_switches_html_renders_rows(client):
    rows = [{"switch": "rabbit-lorax", "driver": "eos",
              "polled_at": "2026-05-24T13:00:00Z",
              "reachable": True, "generation": 412, "port_count": 80}]
    with patch("flax_control.queries.switches", return_value=rows):
        resp = client.get("/switches")
    assert resp.status_code == 200
    body = resp.text
    assert "rabbit-lorax" in body
    assert "eos" in body
    assert "80" in body


def test_switch_detail_html_renders(client):
    sf_ports = [{"port": "Et6/1", "link": "link",
                  "macs": ["aa:bb"], "bmc_mac": "aa:bb",
                  "nic_macs": [], "junk_macs": [],
                  "classification_source": "lldp",
                  "lldp_disagreement": False, "mask": "access"}]
    os_row = {"switch": "rabbit-lorax", "port": "et6b1",
               "vars": {"linkstate": {"value": "link", "since": "t1"},
                        "bmcpower": {"value": "off", "since": "t2"}},
               "last_polled": "...", "generation": 53}
    with patch("flax_control.queries.ports_for_switch", return_value=sf_ports), \
         patch("flax_control.queries.observe_state_one", return_value=os_row):
        resp = client.get("/switches/rabbit-lorax/Et6/1")
    assert resp.status_code == 200
    body = resp.text
    assert "Et6/1" in body
    assert "aa:bb" in body
    assert "link" in body


def test_switch_detail_orders_observe_vars_newest_first(client):
    sf_ports = [{"port": "Et6/1", "link": "link",
                  "macs": ["aa:bb"], "bmc_mac": "aa:bb",
                  "nic_macs": [], "junk_macs": [],
                  "classification_source": "lldp",
                  "lldp_disagreement": False, "mask": "access"}]
    # Out-of-order `since`; one null. Newest = varNewest, then varMiddle,
    # then varOldest, then varNull (null `since` last).
    os_row = {"switch": "rabbit-lorax", "port": "et6b1",
               "vars": {"varOldest": {"value": "x", "since": "2026-01-01T00:00:00Z"},
                        "varNewest": {"value": "x", "since": "2026-06-01T00:00:00Z"},
                        "varNull": {"value": "x", "since": None},
                        "varMiddle": {"value": "x", "since": "2026-03-01T00:00:00Z"}},
               "last_polled": "...", "generation": 53}
    status = {"converged": True, "live_mismatches": 0, "macs": []}
    with patch("flax_control.queries.ports_for_switch", return_value=sf_ports), \
         patch("flax_control.queries.observe_state_one", return_value=os_row), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_status_for_port", return_value=status):
        resp = client.get("/switches/rabbit-lorax/Et6/1")
    assert resp.status_code == 200
    body = resp.text
    assert (body.index("varNewest") < body.index("varMiddle")
            < body.index("varOldest") < body.index("varNull"))


# ---------------------------------------------------------------------------
# reconcile_status_for_port — LIVE lease-vs-reservation truth for a port.
# Converged := every reserved mac on the port has an active lease whose IP
# equals its reservation IP. A mac with a reservation but no active lease
# (lease is None) counts as a live mismatch.
# ---------------------------------------------------------------------------

def _cursor_mock(rows):
    from unittest.mock import MagicMock
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, cursor


def test_reconcile_status_converged_when_lease_matches_reservation(monkeypatch):
    from flax_control import queries
    pool, _ = _cursor_mock([
        ("aa:bb:cc:00:00:01", "172.17.6.101", "172.17.6.101"),
    ])
    monkeypatch.setattr(queries, "get_pool", lambda: pool)
    out = queries.reconcile_status_for_port("rabbit-fiesta", "Ethernet6/1")
    assert out["converged"] is True
    assert out["live_mismatches"] == 0
    assert out["macs"] == [
        {"mac": "aa:bb:cc:00:00:01",
         "reservation": "172.17.6.101", "lease": "172.17.6.101"},
    ]


def test_reconcile_status_mismatch_when_lease_differs(monkeypatch):
    from flax_control import queries
    pool, _ = _cursor_mock([
        ("aa:bb:cc:00:00:01", "172.17.6.101", "172.17.6.250"),
    ])
    monkeypatch.setattr(queries, "get_pool", lambda: pool)
    out = queries.reconcile_status_for_port("rabbit-fiesta", "Ethernet6/1")
    assert out["converged"] is False
    assert out["live_mismatches"] == 1


def test_reconcile_status_no_lease_counts_as_mismatch(monkeypatch):
    from flax_control import queries
    pool, _ = _cursor_mock([
        ("aa:bb:cc:00:00:01", "172.17.6.101", None),
    ])
    monkeypatch.setattr(queries, "get_pool", lambda: pool)
    out = queries.reconcile_status_for_port("rabbit-fiesta", "Ethernet6/1")
    assert out["converged"] is False
    assert out["live_mismatches"] == 1


def test_reconcile_status_empty_port_has_no_macs(monkeypatch):
    from flax_control import queries
    pool, _ = _cursor_mock([])
    monkeypatch.setattr(queries, "get_pool", lambda: pool)
    out = queries.reconcile_status_for_port("rabbit-fiesta", "Ethernet6/1")
    # No reservation for this port: nothing to be out of sync, so converged
    # is vacuously True, but macs is empty so the template shows the muted
    # "no reservation" note rather than a green pill.
    assert out["converged"] is True
    assert out["live_mismatches"] == 0
    assert out["macs"] == []


def test_reconcile_status_passes_internal_port_to_query(monkeypatch):
    """The route hands the Arista URL port (Et6/1, triage_compat's accepted
    form); the query must filter on the INTERNAL short form (et6b1) since
    kea.hosts.classify.port stores the internal form."""
    from flax_control import queries
    pool, cursor = _cursor_mock([])
    monkeypatch.setattr(queries, "get_pool", lambda: pool)
    queries.reconcile_status_for_port("rabbit-fiesta", "Et6/1")
    params = cursor.execute.call_args[0][1]
    assert params["switch"] == "rabbit-fiesta"
    assert params["port_internal"] == "et6b1"


def test_switch_detail_shows_converged_banner(client):
    sf_ports = [{"port": "Et6/1", "link": "link", "macs": ["aa:bb"],
                  "bmc_mac": "aa:bb", "nic_macs": [], "junk_macs": [],
                  "classification_source": "lldp",
                  "lldp_disagreement": False, "mask": "access"}]
    os_row = {"switch": "rabbit-lorax", "port": "et6b1", "vars": {},
               "last_polled": "...", "generation": 53}
    status = {"converged": True, "live_mismatches": 0,
              "macs": [{"mac": "aa:bb", "reservation": "1.2.3.4", "lease": "1.2.3.4"}]}
    with patch("flax_control.queries.ports_for_switch", return_value=sf_ports), \
         patch("flax_control.queries.observe_state_one", return_value=os_row), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_status_for_port", return_value=status):
        resp = client.get("/switches/rabbit-lorax/Et6/1")
    assert resp.status_code == 200
    assert "converged" in resp.text
    assert "lease matches reservation" in resp.text


def test_switch_detail_shows_mismatch_banner(client):
    sf_ports = [{"port": "Et6/1", "link": "link", "macs": ["aa:bb"],
                  "bmc_mac": "aa:bb", "nic_macs": [], "junk_macs": [],
                  "classification_source": "lldp",
                  "lldp_disagreement": False, "mask": "access"}]
    os_row = {"switch": "rabbit-lorax", "port": "et6b1", "vars": {},
               "last_polled": "...", "generation": 53}
    status = {"converged": False, "live_mismatches": 2,
              "macs": [{"mac": "aa:bb", "reservation": "1.2.3.4", "lease": None}]}
    with patch("flax_control.queries.ports_for_switch", return_value=sf_ports), \
         patch("flax_control.queries.observe_state_one", return_value=os_row), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_status_for_port", return_value=status):
        resp = client.get("/switches/rabbit-lorax/Et6/1")
    assert resp.status_code == 200
    assert "2 live mismatch" in resp.text


def test_switch_detail_status_query_returning_none_does_not_500(client):
    """The route must not crash if the status helper returns a falsy value."""
    sf_ports = [{"port": "Et6/1", "link": "link", "macs": [],
                  "bmc_mac": None, "nic_macs": [], "junk_macs": [],
                  "classification_source": "lldp",
                  "lldp_disagreement": False, "mask": "access"}]
    with patch("flax_control.queries.ports_for_switch", return_value=sf_ports), \
         patch("flax_control.queries.observe_state_one", return_value=None), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_status_for_port", return_value=None):
        resp = client.get("/switches/rabbit-lorax/Et6/1")
    assert resp.status_code == 200


def test_events_html_renders_rows(client):
    rows = [{"id": 1, "ts": "2026-05-24T13:00:00Z",
              "service": "flax-observe", "kind": "transition",
              "mac": None, "switch": "rabbit-lorax", "port": "et6b1",
              "payload": {"var": "linkstate", "from": "unknown", "to": "link"}}]
    with patch("flax_control.queries.events", return_value=rows), \
         patch("flax_control.queries.events_facets",
               return_value={"services": [], "kinds": [], "switches": []}):
        resp = client.get("/events")
    assert resp.status_code == 200
    body = resp.text
    assert "transition" in body
    assert "flax-observe" in body
    assert "rabbit-lorax" in body


def test_services_html_renders_ack_rows(client):
    rows = [{"consumer": "flax-observe", "source": "switch_facts",
              "generation": 412, "action": "applied",
              "consumed_at": "2026-05-24T13:00:00Z", "detail": None}]
    with patch("flax_control.queries.consumer_acks", return_value=rows):
        resp = client.get("/services")
    assert resp.status_code == 200
    body = resp.text
    assert "flax-observe" in body
    assert "switch_facts" in body
    assert "applied" in body


# ---------------------------------------------------------------------------
# consumer_health: per-expected-consumer freshness rollup over the fixed 5
# ---------------------------------------------------------------------------

def test_consumer_health_fresh_applied_is_healthy():
    from flax_control.app import consumer_health, EXPECTED_CONSUMERS
    now = "2026-06-19T12:00:00+00:00"
    acks = [{"consumer": "flax-observe", "source": "switch_facts",
             "generation": 7, "action": "applied",
             "consumed_at": "2026-06-19T11:59:30+00:00", "detail": None}]
    health = consumer_health(acks, now_iso=now)
    by = {h["consumer"]: h for h in health}
    assert by["flax-observe"]["state"] == "healthy"
    # All five expected consumers are always present.
    assert {h["consumer"] for h in health} == set(EXPECTED_CONSUMERS)
    assert len(health) == 5


def test_consumer_health_old_applied_is_stale():
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    acks = [{"consumer": "flax-observe", "source": "switch_facts",
             "generation": 7, "action": "applied",
             "consumed_at": "2026-06-19T10:00:00+00:00", "detail": None}]
    by = {h["consumer"]: h for h in consumer_health(acks, now_iso=now)}
    assert by["flax-observe"]["state"] == "stale"


def test_consumer_health_failed_is_failed():
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    acks = [{"consumer": "flax-discover", "source": "observe_state",
             "generation": 3, "action": "failed",
             "consumed_at": "2026-06-19T11:59:55+00:00", "detail": "boom"}]
    by = {h["consumer"]: h for h in consumer_health(acks, now_iso=now)}
    assert by["flax-discover"]["state"] == "failed"


def test_consumer_health_missing_when_no_row():
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    by = {h["consumer"]: h for h in consumer_health([], now_iso=now)}
    assert by["flax-reconcile"]["state"] == "missing"
    assert by["flax-reconcile"]["consumed_at"] is None


def test_consumer_health_picks_latest_row_per_consumer():
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    acks = [
        {"consumer": "flax-observe", "source": "a", "generation": 1,
         "action": "applied", "consumed_at": "2026-06-19T10:00:00+00:00",
         "detail": None},
        {"consumer": "flax-observe", "source": "b", "generation": 2,
         "action": "applied", "consumed_at": "2026-06-19T11:59:50+00:00",
         "detail": None},
    ]
    by = {h["consumer"]: h for h in consumer_health(acks, now_iso=now)}
    # Latest (source b, fresh) wins -> healthy, source b.
    assert by["flax-observe"]["state"] == "healthy"
    assert by["flax-observe"]["source"] == "b"


def test_consumer_health_unparseable_consumed_at_is_stale():
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    acks = [{"consumer": "flax-classify", "source": "observe_state",
             "generation": 1, "action": "applied",
             "consumed_at": None, "detail": None}]
    by = {h["consumer"]: h for h in consumer_health(acks, now_iso=now)}
    assert by["flax-classify"]["state"] == "stale"


def test_consumer_health_idle_reconcile_uses_wider_window():
    """flax-reconcile only acks ~every 900s when the lab is idle, so its
    freshness window is per-consumer (wider). A ~900s-old reconcile ack is
    HEALTHY, while a fast service (flax-observe) with the same age is STALE."""
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    # 900s before now: > the 120s default window, < reconcile's 1080s window.
    old = "2026-06-19T11:45:00+00:00"
    acks = [
        {"consumer": "flax-reconcile", "source": "desired_port",
         "generation": 4, "action": "applied", "consumed_at": old,
         "detail": None},
        {"consumer": "flax-observe", "source": "switch_facts",
         "generation": 4, "action": "applied", "consumed_at": old,
         "detail": None},
    ]
    by = {h["consumer"]: h for h in consumer_health(acks, now_iso=now)}
    assert by["flax-reconcile"]["state"] == "healthy"
    assert by["flax-observe"]["state"] == "stale"


def test_consumer_health_partial_source_failure_is_degraded():
    """switch-sense fans out one ack row per switch (source switches/<name>).
    One switch failing while another is healthy must read DEGRADED and surface
    the failing switch's detail — NOT be masked by the healthy switch's newer
    'applied' ack (the bug a single shared 'switches' row caused)."""
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    acks = [
        {"consumer": "flax-switch-sense", "source": "switches/rabbit-lorax",
         "generation": 10, "action": "applied",
         "consumed_at": "2026-06-19T11:59:55+00:00", "detail": None},
        {"consumer": "flax-switch-sense", "source": "switches/turtle-lorax",
         "generation": 8, "action": "failed",
         "consumed_at": "2026-06-19T11:59:50+00:00",
         "detail": "turtle-lorax: 'net show interface all json' timed out"},
    ]
    by = {h["consumer"]: h for h in consumer_health(acks, now_iso=now)}
    h = by["flax-switch-sense"]
    assert h["state"] == "degraded"
    assert h["source"] == "switches/turtle-lorax"
    assert "turtle-lorax" in (h["detail"] or "")


def test_consumer_health_all_sources_failed_is_failed():
    """Every switch failing is a full failure, not merely degraded."""
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    acks = [
        {"consumer": "flax-switch-sense", "source": "switches/rabbit-lorax",
         "generation": 10, "action": "failed",
         "consumed_at": "2026-06-19T11:59:55+00:00", "detail": "boom1"},
        {"consumer": "flax-switch-sense", "source": "switches/turtle-lorax",
         "generation": 8, "action": "failed",
         "consumed_at": "2026-06-19T11:59:50+00:00", "detail": "boom2"},
    ]
    by = {h["consumer"]: h for h in consumer_health(acks, now_iso=now)}
    assert by["flax-switch-sense"]["state"] == "failed"


def test_consumer_health_degraded_needs_a_real_failure():
    """A stale (not failed) source among healthy ones is NOT degraded — degraded
    is failure-driven; stale-mixing falls back to latest-wins."""
    from flax_control.app import consumer_health
    now = "2026-06-19T12:00:00+00:00"
    acks = [
        {"consumer": "flax-switch-sense", "source": "switches/rabbit-lorax",
         "generation": 10, "action": "applied",
         "consumed_at": "2026-06-19T11:59:55+00:00", "detail": None},
        {"consumer": "flax-switch-sense", "source": "switches/turtle-lorax",
         "generation": 8, "action": "applied",
         "consumed_at": "2026-06-19T09:00:00+00:00", "detail": None},  # stale
    ]
    by = {h["consumer"]: h for h in consumer_health(acks, now_iso=now)}
    # latest (rabbit, fresh) wins -> healthy, not degraded
    assert by["flax-switch-sense"]["state"] == "healthy"


def test_index_pipeline_tile_shows_over_5(client):
    """Hero acking badge numerator = healthy consumers, denominator fixed 5.

    Task 5: the old Pipeline-tile "N<small>/5 healthy</small>" copy was
    dropped with the fleet-card overview in favour of the pipeline hero's
    "N/5 acking" badge (dashboard_view.build_pipeline + index()); this test's
    intent (healthy-count-of-5 renders correctly) is unchanged, only the copy.
    """
    acks = [{"consumer": "flax-observe", "source": "switch_facts",
             "generation": 7, "action": "applied",
             "consumed_at": "2026-06-19T11:59:55+00:00", "detail": None},
            {"consumer": "flax-discover", "source": "observe_state",
             "generation": 3, "action": "noop",
             "consumed_at": "2026-06-19T11:59:50+00:00", "detail": None}]
    with patch("flax_control.queries.switches", return_value=[]), \
         patch("flax_control.queries.observe_state_all", return_value={}), \
         patch("flax_control.queries.consumer_acks", return_value=acks), \
         patch("flax_control.queries.events", return_value=[]), \
         patch("flax_control.queries.leases", return_value=[]), \
         patch("flax_control.queries.reservations", return_value=[]), \
         patch("flax_control.queries.desired_by_role_kind", return_value=[]), \
         patch("flax_control.app._now_iso",
               return_value="2026-06-19T12:00:00+00:00"):
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Two fresh-clean acks out of the fixed 5.
    assert "2/5 acking" in body


def test_services_lists_all_five_when_acks_empty(client):
    """The /services page always lists all 5 expected consumers, even with no
    acks (all 'missing')."""
    from flax_control.app import EXPECTED_CONSUMERS
    with patch("flax_control.queries.consumer_acks", return_value=[]):
        resp = client.get("/services")
    assert resp.status_code == 200
    body = resp.text
    for c in EXPECTED_CONSUMERS:
        assert c in body
    assert "missing" in body


def test_index_renders_health_rollup(client):
    """Task 5: the old Pipeline/Fleet/Switches overview cards (incl. the
    family-breakdown fleet block) were replaced by the pipeline hero + layer
    cards. Re-targeted to the new hero: the pipeline stage service names still
    surface (flax-observe), and the Sensing layer card's reachable-switches
    figure reflects queries.switches(). The old Fleet-card/family-breakdown
    assertions had no successor (that block was dropped outright) and are
    retired rather than replaced."""
    with patch("flax_control.queries.switches",
                return_value=[{"switch": "rabbit-lorax", "reachable": True,
                                "port_count": 80, "driver": "eos",
                                "polled_at": "2026-06-17T10:00:00", "generation": 1}]), \
         patch("flax_control.queries.observe_state_all",
                return_value={"a:b": {}, "a:c": {}}), \
         patch("flax_control.queries.consumer_acks",
                return_value=[{"consumer": "flax-observe", "source": "switch_facts",
                               "generation": 412, "action": "applied",
                               "consumed_at": "...", "detail": None}]), \
         patch("flax_control.queries.events",
                return_value=[{"id": 1, "ts": "...", "service": "flax-observe",
                                "kind": "transition", "mac": None,
                                "switch": "rabbit-lorax", "port": "et6b1",
                                "payload": {}}]), \
         patch("flax_control.queries.leases", return_value=[]), \
         patch("flax_control.queries.reservations", return_value=[]), \
         patch("flax_control.queries.desired_by_role_kind", return_value=[]):
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Hero + layer-axis headings.
    assert "System status" in body
    assert "Sensing" in body
    # Pipeline stage surfaces the consumer/service name.
    assert "flax-observe" in body
    # Sensing layer card: 1 reachable of 1 known switch.
    assert "1/1" in body


def test_devices_html_renders_enrolled_rows(client):
    """/devices page lists each enrolled device with mac, switch, port, kind, family."""
    rows = [
        {"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-fiesta", "port": "et6b1",
         "kind": "bmc", "family": "tiogapass", "last_seen": "2026-06-17T10:00:00"},
        {"mac": "aa:bb:cc:00:00:02", "switch": "rabbit-fiesta", "port": "et6b2",
         "kind": "host", "family": None, "last_seen": "2026-06-17T10:01:00"},
    ]
    with patch("flax_control.queries.devices", return_value=rows):
        resp = client.get("/devices")
    assert resp.status_code == 200
    body = resp.text
    assert "aa:bb:cc:00:00:01" in body
    assert "rabbit-fiesta" in body
    assert "et6b1" in body
    assert "tiogapass" in body
    assert "aa:bb:cc:00:00:02" in body


def test_devices_html_renders_empty_state(client):
    """/devices with no rows shows the enrolled-yet message."""
    with patch("flax_control.queries.devices", return_value=[]):
        resp = client.get("/devices")
    assert resp.status_code == 200
    assert b"No devices enrolled yet" in resp.content


def test_device_detail_html_renders_cross_service_sections(client):
    """/devices/<mac> renders reservation, observe, port_facts, desired, events."""
    full = {
        "mac": "aa:bb:cc:00:00:01",
        "switch": "rabbit-fiesta",
        "port": "et6b1",
        "kind": "bmc",
        "latched": {"family": "tiogapass"},
        "last_seen": "2026-06-17T10:00:00",
        "reservation": {
            "ipv4_address": "172.17.6.101",
            "hostname": "leopard6b1-bmc",
            "subnet_id": 17,
            "classify": {"vid": 17},
            "operator_note": "all good",
        },
        "observe": {
            "vars": {"linkstate": {"value": "link", "since": "t1"},
                     "bmcpower": {"value": "on", "since": "t2"}},
            "last_polled": "2026-06-17T09:59:00",
            "generation": 53,
            "resolved": {"bmc_mac": "aa:bb:cc:00:00:01", "bmc_ip": "10.0.0.1"},
        },
        "port_facts": {
            "link": "link", "mask": "access",
            "macs": ["aa:bb:cc:00:00:01"], "bmc_mac": "aa:bb:cc:00:00:01",
            "nic_macs": [], "junk_macs": [],
            "classification_source": "lldp", "lldp_disagreement": False,
            "lldp_neighbors": [],
        },
        "desired": {"desired_vid": 17, "occupants": [], "generation": 12},
        "last_reconcile_action": {
            "ts": "2026-06-17T09:50:00", "action": "switch_flap",
            "outcome": "success", "reason": "mismatch", "detail": {},
        },
        "recent_events": [
            {"id": 1, "ts": "2026-06-17T09:55:00", "service": "flax-observe",
             "kind": "transition", "switch": "rabbit-fiesta", "port": "et6b1",
             "payload": {}},
        ],
    }
    actions = [
        {"ts": "2026-06-17T09:50:00", "action": "switch_flap",
         "outcome": "success", "reason": "mismatch", "detail": {}},
    ]
    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=actions), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    # Identity
    assert "aa:bb:cc:00:00:01" in body
    assert "rabbit-fiesta" in body
    assert "et6b1" in body
    # Reservation
    assert "172.17.6.101" in body
    assert "leopard6b1-bmc" in body
    assert "all good" in body
    # Observe
    assert "linkstate" in body
    assert "bmcpower" in body
    # Switch facts
    assert "lldp" in body
    # Desired
    assert "17" in body
    # Recent events
    assert "flax-observe" in body
    assert "transition" in body


def _device_detail_full(port="et6b1", switch="rabbit-fiesta"):
    return {
        "mac": "aa:bb:cc:00:00:01", "switch": switch, "port": port,
        "kind": "bmc", "latched": {"family": "tiogapass"},
        "last_seen": "2026-06-17T10:00:00",
        "reservation": None, "observe": None, "port_facts": None,
        "desired": None, "last_reconcile_action": None, "recent_events": [],
    }


def test_device_detail_shows_triage_fw_link_for_geometry_port(client):
    """A device on a triage-geometry port links to /bmc-fw-triage."""
    geometry = [{"port": "et6b1", "switch": "rabbit-fiesta", "ou": "20L"}]
    with patch("flax_control.queries.device_full", return_value=_device_detail_full()), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None), \
         patch("flax_control.app._load_geometry", return_value=geometry):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    assert 'href="/bmc-fw-triage"' in resp.text


def test_device_detail_shows_post_fw_link_for_non_geometry_port(client):
    """A device on a port absent from geometry.json (post) links to /bmc-fw-post."""
    with patch("flax_control.queries.device_full",
               return_value=_device_detail_full(port="et10b1")), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None), \
         patch("flax_control.app._load_geometry", return_value=[]):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    assert 'href="/bmc-fw-post"' in resp.text


def test_device_detail_post_device_same_port_token_as_triage_geometry(client):
    """Regression: eindhoven triage (rabbit-gouda) and post (rabbit-edam)
    switches reuse the SAME port tokens (e.g. et10b1). A post device on
    rabbit-edam/et10b1 must NOT be misrouted to /bmc-fw-triage just because
    geometry.json happens to record et10b1 for the triage switch
    rabbit-gouda -- the match must be scoped to the (switch, port) pair."""
    geometry = [{"port": "et10b1", "switch": "rabbit-gouda", "ou": "20L"}]
    with patch("flax_control.queries.device_full",
               return_value=_device_detail_full(port="et10b1", switch="rabbit-edam")), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None), \
         patch("flax_control.app._load_geometry", return_value=geometry):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    assert 'href="/bmc-fw-post"' in resp.text
    assert 'href="/bmc-fw-triage"' not in resp.text


def test_device_detail_triage_device_same_port_token_matches_own_switch(client):
    """Mirror of the collision test: a triage device on rabbit-gouda/et10b1
    (the switch geometry.json actually records for et10b1) still links to
    /bmc-fw-triage."""
    geometry = [{"port": "et10b1", "switch": "rabbit-gouda", "ou": "20L"}]
    with patch("flax_control.queries.device_full",
               return_value=_device_detail_full(port="et10b1", switch="rabbit-gouda")), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None), \
         patch("flax_control.app._load_geometry", return_value=geometry):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    assert 'href="/bmc-fw-triage"' in resp.text


def test_device_detail_normalises_hex_mac_input(client):
    """/devices/<bare-hex> must normalise to colon-form before device_full
    (backward-compat: legacy hex links still resolve)."""
    captured = {}

    def _device_full(mac):
        captured["mac"] = mac
        return None

    with patch("flax_control.queries.device_full", side_effect=_device_full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None):
        resp = client.get("/devices/1c34da7f9d32")
    assert resp.status_code == 200
    assert captured["mac"] == "1c:34:da:7f:9d:32"


def test_device_detail_normalises_colon_mac_input(client):
    """A colon-form path arg resolves unchanged."""
    captured = {}

    def _device_full(mac):
        captured["mac"] = mac
        return None

    with patch("flax_control.queries.device_full", side_effect=_device_full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None):
        resp = client.get("/devices/1C:34:DA:7F:9D:32")
    assert resp.status_code == 200
    assert captured["mac"] == "1c:34:da:7f:9d:32"


def test_device_detail_renders_ipv6_link_local(client):
    """The device page shows fe80::EUI64%eth0.<vid> from the reservation vid."""
    full = {
        "mac": "1c:34:da:7f:9d:32",
        "switch": "rabbit-fiesta", "port": "et6b1", "kind": "host",
        "latched": {}, "last_seen": "2026-06-17T10:00:00",
        "reservation": {"ipv4_address": "172.16.7.1", "hostname": "leopard7b1",
                        "subnet_id": 16, "classify": {"vid": 16},
                        "operator_note": None},
        "observe": None, "port_facts": None, "desired": None,
        "last_reconcile_action": None, "recent_events": [],
    }
    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None):
        resp = client.get("/devices/1c:34:da:7f:9d:32")
    assert resp.status_code == 200
    assert "fe80::1e34:daff:fe7f:9d32%eth0.16" in resp.text


def test_device_detail_html_not_found_renders_gracefully(client):
    """/devices/<mac> when device_full returns None still renders the page."""
    with patch("flax_control.queries.device_full", return_value=None), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None):
        resp = client.get("/devices/ff:ff:ff:ff:ff:ff")
    assert resp.status_code == 200
    assert b"ff:ff:ff:ff:ff:ff" in resp.content
    assert b"No device record found" in resp.content


def test_leases_html_renders_active_leases(client):
    """/leases lists active leases with mac->device links + subnet summary."""
    rows = [
        {"mac": "aa:bb:cc:00:00:01", "ip": "172.17.6.2", "subnet_id": 17,
         "expires": "2026-06-17T12:00:00", "state": 0},
        {"mac": "aa:bb:cc:00:00:02", "ip": "172.17.6.10", "subnet_id": 17,
         "expires": "2026-06-17T12:00:00", "state": 0},
    ]
    with patch("flax_control.queries.leases", return_value=rows), \
         patch("flax_control.queries.reservations", return_value=[]):
        resp = client.get("/leases")
    assert resp.status_code == 200
    body = resp.text
    assert "172.17.6.2" in body
    assert "aa:bb:cc:00:00:01" in body
    assert "/devices/aa:bb:cc:00:00:01" in body   # mac cross-links to device
    assert "subnet 17: 2" in body                 # per-subnet summary


def test_leases_html_renders_empty_state(client):
    with patch("flax_control.queries.leases", return_value=[]), \
         patch("flax_control.queries.reservations", return_value=[]):
        resp = client.get("/leases")
    assert resp.status_code == 200
    assert b"No active leases" in resp.content


# ---------------------------------------------------------------------------
# device_detail_page: op_request banners, disabled buttons, auto-refresh
# ---------------------------------------------------------------------------

def _full_device(observe_vars=None):
    """Return a minimal device_full dict for device detail tests."""
    if observe_vars is None:
        observe_vars = {"linkstate": {"value": "link", "since": "t1"},
                        "bmcipmi": {"value": "ok", "since": "t2"},
                        "bmcping": {"value": "ok", "since": "t3"}}
    return {
        "mac": "aa:bb:cc:00:00:01",
        "switch": "rabbit-fiesta",
        "port": "et6b1",
        "kind": "bmc",
        "latched": {"family": "tiogapass"},
        "last_seen": "2026-06-17T10:00:00",
        "reservation": None,
        "observe": {
            "vars": observe_vars,
            "last_polled": "2026-06-17T09:59:00",
            "generation": 53,
            "resolved": {},
        },
        "port_facts": None,
        "desired": None,
        "last_reconcile_action": None,
        "recent_events": [],
    }


def test_device_detail_op_request_passed_to_template(client):
    """device_detail_page calls reconcile_request_for_mac and passes op_request to template."""
    full = _full_device()
    op_req = {"reason": "operator flap", "status": "pending",
               "attempts": 0, "outcome": None,
               "ts": "2026-06-17T10:00:00", "completed_at": None}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200


def test_device_detail_bmc_reset_in_progress_shows_info_banner(client):
    """BMC reset pending: info banner displayed, buttons disabled."""
    full = _full_device()
    op_req = {"reason": "operator bmc-reset", "status": "pending",
               "attempts": 0, "outcome": None,
               "ts": "2026-06-17T10:00:00", "completed_at": None}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    # Info banner present
    assert "Reset BMC via Redfish requested" in body
    # Both buttons disabled
    assert body.count("disabled") >= 2


def test_device_detail_bmc_reset_claimed_shows_info_banner(client):
    """BMC reset claimed (in-flight): info banner, buttons disabled."""
    full = _full_device()
    op_req = {"reason": "operator bmc-reset", "status": "claimed",
               "attempts": 1, "outcome": None,
               "ts": "2026-06-17T10:00:00", "completed_at": None}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert "Reset BMC via Redfish requested" in body
    assert body.count("disabled") >= 2


def test_device_detail_bmc_reset_done_but_ipmi_still_unknown_shows_awaiting_banner(client):
    """BMC reset done but bmcipmi still unknown: awaiting-recovery banner, buttons disabled."""
    observe_vars = {"linkstate": {"value": "link", "since": "t1"},
                    "bmcipmi": {"value": "unknown", "since": "t2"},
                    "bmcping": {"value": "ok", "since": "t3"}}
    full = _full_device(observe_vars=observe_vars)
    op_req = {"reason": "operator bmc-reset", "status": "done",
               "attempts": 1, "outcome": "reset sent",
               "ts": "2026-06-17T10:00:00", "completed_at": "2026-06-17T10:01:00"}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert "BMC reset sent" in body
    assert "waiting for the BMC" in body
    # Buttons still disabled while awaiting recovery
    assert body.count("disabled") >= 2


def test_device_detail_bmc_reset_stuck_shows_failed_banner_and_reenables(client):
    """BMC reset stuck: failure banner shown, reset button re-enabled."""
    full = _full_device()
    op_req = {"reason": "operator bmc-reset", "status": "stuck",
               "attempts": 3, "outcome": "Redfish unreachable",
               "ts": "2026-06-17T10:00:00", "completed_at": None}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert "BMC reset failed" in body
    assert "3" in body   # attempts count shown
    # Not all buttons disabled (reset is re-enabled)
    disabled_count = body.count("disabled")
    # At most one button disabled (the flap button may or may not be disabled,
    # but the reset button must be re-enabled → less than 2 total disables)
    assert disabled_count < 2


def test_device_detail_flap_in_progress_shows_info_banner_and_disables_buttons(client):
    """Operator flap pending: reconcile-in-progress banner, both buttons disabled."""
    full = _full_device()
    op_req = {"reason": "operator flap", "status": "pending",
               "attempts": 0, "outcome": None,
               "ts": "2026-06-17T10:00:00", "completed_at": None}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert "Reconcile" in body
    assert "in progress" in body
    assert body.count("disabled") >= 2


def test_device_detail_flap_stuck_shows_failed_banner(client):
    """Operator flap stuck: failure banner shown."""
    full = _full_device()
    op_req = {"reason": "operator flap", "status": "stuck",
               "attempts": 2, "outcome": "timeout",
               "ts": "2026-06-17T10:00:00", "completed_at": None}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert "Reconcile failed" in body
    assert "2" in body   # attempts count


def test_device_detail_idle_no_banner_buttons_enabled(client):
    """Idle state (no op_request): no action banner, buttons enabled."""
    full = _full_device()

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    # No action banners
    assert "Reset BMC via Redfish requested" not in body
    assert "BMC reset sent" not in body
    assert "BMC reset failed" not in body
    assert "Reconcile" not in body or "in progress" not in body
    # Buttons enabled (not disabled by action state)
    # The only disabled button should be the "no device" guard — not present here
    assert "disabled" not in body


def test_device_detail_idle_bmc_stuck_shows_session_exhausted_banner(client):
    """Idle + bmcipmi=unknown + bmcping=ok: session-exhausted banner shown."""
    observe_vars = {"bmcipmi": {"value": "unknown", "since": "t1"},
                    "bmcping": {"value": "ok", "since": "t2"}}
    full = _full_device(observe_vars=observe_vars)

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert "session-exhausted" in body


def test_device_detail_in_progress_adds_auto_refresh(client):
    """When an action is in-progress, a meta http-equiv refresh tag is in the head."""
    full = _full_device()
    op_req = {"reason": "operator flap", "status": "pending",
               "attempts": 0, "outcome": None,
               "ts": "2026-06-17T10:00:00", "completed_at": None}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert 'http-equiv="refresh"' in body or "http-equiv='refresh'" in body


def test_device_detail_awaiting_recovery_adds_auto_refresh(client):
    """BMC reset done but ipmi still unknown: auto-refresh tag present."""
    observe_vars = {"bmcipmi": {"value": "unknown", "since": "t1"},
                    "bmcping": {"value": "ok", "since": "t2"}}
    full = _full_device(observe_vars=observe_vars)
    op_req = {"reason": "operator bmc-reset", "status": "done",
               "attempts": 1, "outcome": "reset sent",
               "ts": "2026-06-17T10:00:00", "completed_at": "2026-06-17T10:01:00"}

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=op_req):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert 'http-equiv="refresh"' in body or "http-equiv='refresh'" in body


def test_device_detail_idle_no_auto_refresh(client):
    """Idle state (no op_request): no auto-refresh meta tag."""
    full = _full_device()

    with patch("flax_control.queries.device_full", return_value=full), \
         patch("flax_control.queries.reconcile_actions_for_port", return_value=[]), \
         patch("flax_control.queries.reconcile_request_for_mac", return_value=None):
        resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    body = resp.text
    assert 'http-equiv="refresh"' not in body
    assert "http-equiv='refresh'" not in body


def test_bmc_fw_page_renders_tp_rows(client):
    state = {"rabbit-gouda:Ethernet10/3": {
        "switch": "rabbit-gouda", "port": "Ethernet10/3", "vars": {},
        "last_polled": "x", "generation": 1,
        "resolved": {"product_name": "Wiwynn Tioga Pass",
                     "bmc_ip": "172.17.10.103", "chassis_sn": "WTH-A"}}}
    manifest = {"tiogapass-openbmc": {
        "match": {"product_name": ["(?i)tioga"]},
        "target_version": "flax-onetree-1.0.6-202606261452", "auto": True}}
    with patch("flax_control.queries.observe_state_all", return_value=state), \
         patch("flax_control.bmcfw_view._read_json",
               side_effect=lambda name: manifest if "versions" in name else None):
        resp = client.get("/bmc-fw-triage")  # route moved from /bmc-fw (Task 5)
    assert resp.status_code == 200
    assert "Et10/3" in resp.text
    assert "flax-onetree-1.0.6-202606261452" in resp.text
    assert "not evaluated" in resp.text  # store empty -> unevaluated row


def test_bmc_fw_page_renders_zero_percent(client):
    state = {"rabbit-gouda:Ethernet10/3": {
        "switch": "rabbit-gouda", "port": "Ethernet10/3", "vars": {},
        "last_polled": "x", "generation": 1,
        "resolved": {"product_name": "Wiwynn Tioga Pass",
                     "bmc_ip": "172.17.10.103", "chassis_sn": "WTH-A"}}}
    manifest = {"tiogapass-openbmc": {
        "match": {"product_name": ["(?i)tioga"]},
        "target_version": "flax-onetree-1.0.6-202606261452", "auto": True}}
    store = {"et10b3": {"port": "et10b3", "phase": "flashing", "percent": 0,
                        "current_version": "flax-onetree-1.0.6-202606221107",
                        "target_version": "flax-onetree-1.0.6-202606261452",
                        "serial": "WTH-A", "bmc_ip": "172.17.10.103",
                        "fault_reason": "", "updated_at": 1.0}}
    def fake_read(name):
        return manifest if "versions" in name else store
    with patch("flax_control.queries.observe_state_all", return_value=state), \
         patch("flax_control.bmcfw_view._read_json", side_effect=fake_read):
        resp = client.get("/bmc-fw-triage")  # route moved from /bmc-fw (Task 5)
    assert resp.status_code == 200
    assert "0%" in resp.text


def test_bmc_fw_page_columns_no_serial_split_versions(client):
    state = {"rabbit-gouda:Ethernet10/3": {
        "switch": "rabbit-gouda", "port": "Ethernet10/3", "vars": {},
        "last_polled": "x", "generation": 1,
        "resolved": {"product_name": "Wiwynn Tioga Pass",
                     "bmc_ip": "172.17.10.103", "chassis_sn": "WTH-A"}}}
    manifest = {"tiogapass-openbmc": {
        "match": {"product_name": ["(?i)tioga"]},
        "target_version": "flax-onetree-1.0.6-202606261452", "auto": True}}
    with patch("flax_control.queries.observe_state_all", return_value=state), \
         patch("flax_control.bmcfw_view._read_json",
               side_effect=lambda name: manifest if "versions" in name else None):
        resp = client.get("/bmc-fw-triage")  # route moved from /bmc-fw (Task 5)
    assert resp.status_code == 200
    assert "<th>serial</th>" not in resp.text
    assert "<th>current</th>" in resp.text
    assert "<th>target</th>" in resp.text
    assert "→" not in resp.text   # combined arrow column gone


def test_bmc_fw_triage_page_shows_store_freshness(client):
    """Task 5: /bmc-fw-triage renders a 'Store updated ... Now ...' freshness
    line, computed in Python (bmcfw_view.fmt_updated), not Jinja."""
    state = {"rabbit-gouda:Ethernet10/3": {
        "switch": "rabbit-gouda", "port": "Ethernet10/3", "vars": {},
        "last_polled": "x", "generation": 1,
        "resolved": {"product_name": "Wiwynn Tioga Pass",
                     "bmc_ip": "172.17.10.103", "chassis_sn": "WTH-A"}}}
    manifest = {"tiogapass-openbmc": {
        "match": {"product_name": ["(?i)tioga"]},
        "target_version": "flax-onetree-1.0.6-202606261452", "auto": True}}
    store = {"et10b3": {"port": "et10b3", "phase": "flashing", "percent": 0,
                        "current_version": "flax-onetree-1.0.6-202606221107",
                        "target_version": "flax-onetree-1.0.6-202606261452",
                        "serial": "WTH-A", "bmc_ip": "172.17.10.103",
                        "fault_reason": "", "updated_at": 1.0}}
    def fake_read(name):
        return manifest if "versions" in name else store
    with patch("flax_control.queries.observe_state_all", return_value=state), \
         patch("flax_control.bmcfw_view._read_json", side_effect=fake_read):
        resp = client.get("/bmc-fw-triage")
    assert resp.status_code == 200
    assert "updated" in resp.text.lower()
    assert "Now" in resp.text


def test_bmc_fw_redirects_to_triage(client):
    """Task 5: /bmc-fw is now a 303 redirect to /bmc-fw-triage (routes are
    frozen for external consumers — it must not 404)."""
    resp = client.get("/bmc-fw", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/bmc-fw-triage"


# ---------------------------------------------------------------------------
# Task 6: /bmc-fw-post — read-only post BMC/NIC/BIOS firmware from file stores
# ---------------------------------------------------------------------------

def test_bmc_fw_post_page_renders_merged_rows(client):
    bmc = {"et10b1": {"port": "et10b1", "bmc_ip": "172.25.10.101",
                       "phase": "up_to_date", "current_version": "1.0.9",
                       "target_version": "1.0.9", "updated_at": 100.0}}
    nic = {"et10b1": {"phase": "mlx-checked", "updated_at": 90.0}}
    bios = {"et10b1": {"phase": "up_to_date", "updated_at": 80.0}}
    with patch("flax_control.post_bmcfw_view.read_stores",
               return_value=(bmc, nic, bios)):
        resp = client.get("/bmc-fw-post")
    assert resp.status_code == 200
    assert "et10b1" in resp.text
    assert "172.25.10.101" in resp.text
    assert "1.0.9" in resp.text
    assert "mlx-checked" in resp.text


def test_bmc_fw_post_page_empty_state(client):
    """Missing/absent post fw stores -> empty state, never a 500 (a bind-mount
    of an absent file becomes a directory; read_stores must tolerate that)."""
    with patch("flax_control.post_bmcfw_view.read_stores",
               return_value=(None, None, None)):
        resp = client.get("/bmc-fw-post")
    assert resp.status_code == 200
    assert "empty" in resp.text.lower() or "no post firmware" in resp.text.lower()


def test_bmc_fw_post_route_registered(client):
    from flax_control.app import app
    registered = {getattr(r, "path", None) for r in app.routes}
    assert "/bmc-fw-post" in registered
