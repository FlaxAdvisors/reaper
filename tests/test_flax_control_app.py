"""Smoke tests for the flax-control app."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from flax_control.app import app
    return TestClient(app)


def test_healthz_returns_200(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}



def test_devices_renders(client, monkeypatch):
    from flax_control import queries
    monkeypatch.setattr(queries, "devices", lambda: [])
    response = client.get("/devices")
    assert response.status_code == 200
    # Design-system migration (Task 3) renamed the page heading from
    # "Devices" to "Nodes" (matches the sidebar nav label); the deprecated
    # bare {% block heading %} is no longer set, page_header() owns the title.
    assert b"Nodes" in response.content
    assert b"No devices enrolled yet" in response.content



def test_api_healthz_ok_when_db_up(client, monkeypatch):
    """When the DB pool returns a working connection, /api/v1/healthz reports ok."""
    from flax_control import db as db_module
    from unittest.mock import MagicMock

    # Mock a working pool that yields a cursor returning 1
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    cursor.fetchone.return_value = (1,)
    conn.cursor.return_value = cursor

    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(db_module, "get_pool", lambda: pool)

    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


def test_api_healthz_fail_when_db_down(client, monkeypatch):
    """When pool raises, /api/v1/healthz returns 503 with db=fail."""
    from flax_control import db as db_module

    def fail_pool():
        raise RuntimeError("db down")
    monkeypatch.setattr(db_module, "get_pool", fail_pool)

    response = client.get("/api/v1/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["db"] == "fail"


def test_api_v1_reservations_returns_json_list(client, monkeypatch):
    """The /api/v1/reservations endpoint returns queries.reservations() output."""
    from flax_control import queries

    monkeypatch.setattr(queries, "reservations", lambda: [{
        "mac": "1c:34:da:7f:b3:a4", "switch": "rabbit-lorax", "port": "et6b1",
        "kind": "bmc", "vid": 17,
        "ipv4_address": "172.17.6.101", "hostname": "leopard6b1-bmc",
        "generation": 3, "updated_at": "2026-05-24T18:00:00+00:00",
    }])
    r = client.get("/api/v1/reservations")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["mac"] == "1c:34:da:7f:b3:a4"
    assert body[0]["ipv4_address"] == "172.17.6.101"


def test_reservations_html_renders_rows(client, monkeypatch):
    """The /reservations HTML page lists each row."""
    from flax_control import queries

    monkeypatch.setattr(queries, "reservations", lambda: [{
        "mac": "1c:34:da:7f:b3:a4", "switch": "rabbit-lorax", "port": "et6b1",
        "kind": "bmc", "vid": 17,
        "ipv4_address": "172.17.6.101", "hostname": "leopard6b1-bmc",
        "generation": 3, "updated_at": "2026-05-24T18:00:00+00:00",
    }])
    r = client.get("/reservations")
    assert r.status_code == 200
    assert b"172.17.6.101" in r.content
    assert b"leopard6b1-bmc" in r.content
    assert b"rabbit-lorax" in r.content


def test_reservations_html_renders_ipv6_and_colon_mac(client, monkeypatch):
    """Reservations page shows the colon-form MAC, links /devices/<colon-mac>,
    shows the ipv6_address column, and posts operator notes with the hex form."""
    from flax_control import queries

    monkeypatch.setattr(queries, "reservations", lambda: [{
        "mac": "1c:34:da:7f:b3:a4", "mac_hex": "1c34da7fb3a4",
        "switch": "rabbit-lorax", "port": "et6b1", "kind": "bmc", "vid": 17,
        "ipv4_address": "172.17.6.101", "ipv6_address": "fd00:17::6:101",
        "hostname": "leopard6b1-bmc", "subnet_id": 17,
        "operator_note": None, "generation": 3,
        "updated_at": "2026-05-24T18:00:00+00:00",
    }])
    r = client.get("/reservations")
    assert r.status_code == 200
    body = r.text
    assert "1c:34:da:7f:b3:a4" in body                  # colon-form display
    assert "/devices/1c:34:da:7f:b3:a4" in body         # device link in colon form
    assert "fd00:17::6:101" in body                     # ipv6 column value
    # operator-note form still posts the strict 12-hex form
    assert "/reservations/1c34da7fb3a4/operator_note" in body


def test_reservations_html_renders_empty_state(client, monkeypatch):
    from flax_control import queries
    monkeypatch.setattr(queries, "reservations", lambda: [])
    r = client.get("/reservations")
    assert r.status_code == 200
    assert b"No proposals yet" in r.content


def test_base_template_has_reservations_nav_link(client, monkeypatch):
    """Any rendered page (e.g. /devices) carries the new nav link."""
    from flax_control import queries
    monkeypatch.setattr(queries, "devices", lambda: [])
    r = client.get("/devices")
    assert r.status_code == 200
    assert b"/reservations" in r.content


def test_patch_operator_note_round_trip(client, monkeypatch):
    """PATCH stores the note; subsequent GET shows it."""
    from flax_control import operator_notes

    captured = {}
    def fake_update(mac_hex, note):
        captured["mac"] = mac_hex
        captured["note"] = note
    monkeypatch.setattr(operator_notes, "update_operator_note", fake_update)

    r = client.patch("/api/v1/reservations/1c34da7fb3a4/operator_note",
                     json={"note": "flaky bmc"})
    assert r.status_code == 200
    body = r.json()
    assert body["mac"] == "1c34da7fb3a4"
    assert body["operator_note"] == "flaky bmc"
    assert captured == {"mac": "1c34da7fb3a4", "note": "flaky bmc"}


def test_patch_operator_note_empty_string_clears(client, monkeypatch):
    from flax_control import operator_notes

    monkeypatch.setattr(operator_notes, "update_operator_note",
                        lambda mac, note: None)
    r = client.patch("/api/v1/reservations/1c34da7fb3a4/operator_note",
                     json={"note": ""})
    assert r.status_code == 200
    assert r.json()["operator_note"] == ""


def test_patch_operator_note_missing_mac_returns_404(client, monkeypatch):
    from flax_control import operator_notes

    def raise_notfound(mac, note):
        raise operator_notes.NotFound(mac)
    monkeypatch.setattr(operator_notes, "update_operator_note", raise_notfound)

    r = client.patch("/api/v1/reservations/deadbeefdead/operator_note",
                     json={"note": "x"})
    assert r.status_code == 404


def test_patch_operator_note_missing_body_returns_400(client):
    r = client.patch("/api/v1/reservations/1c34da7fb3a4/operator_note",
                     json={})
    assert r.status_code == 400


def test_post_operator_note_form_redirects(client, monkeypatch):
    """Form-encoded POST sets the note and 303s back to /reservations."""
    from flax_control import operator_notes
    called = {}
    monkeypatch.setattr(operator_notes, "update_operator_note",
                        lambda mac, note: called.update(mac=mac, note=note))

    r = client.post("/reservations/1c34da7fb3a4/operator_note",
                    data={"note": "ipmi flaky"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/reservations"
    assert called == {"mac": "1c34da7fb3a4", "note": "ipmi flaky"}


def test_reservations_html_renders_operator_note_column(client, monkeypatch):
    from flax_control import queries
    monkeypatch.setattr(queries, "reservations", lambda: [{
        "mac": "1c34da7fb3a4", "switch": "rabbit-lorax", "port": "et6b1",
        "kind": "bmc", "vid": 17,
        "ipv4_address": "172.17.6.101", "hostname": "leopard6b1-bmc",
        "subnet_id": 17,
        "operator_note": "flaky -- monitor",
        "generation": 3, "updated_at": "2026-05-24T18:00:00+00:00",
    }])
    r = client.get("/reservations")
    assert r.status_code == 200
    assert b"flaky -- monitor" in r.content
    assert b'name="note"' in r.content


# ---- /config route tests -----------------------------------------------

def test_config_page_renders_200(client, monkeypatch):
    """The /config page renders successfully with mocked config_view data."""
    from flax_control import config_view, queries
    monkeypatch.setattr(queries, "consumer_acks", lambda: [])
    monkeypatch.setattr(config_view, "reconcile_tunables", lambda: [
        {"key": "sweep_interval_secs", "default": 900, "current": 300,
         "source": "reconcile.json"},
        {"key": "flap_hold_seconds", "default": 2, "current": 2,
         "source": "(default)"},
    ])
    monkeypatch.setattr(config_view, "topology_files", lambda: [
        {"path": "/etc/flax/geometry.json", "drives": "triage ports",
         "deployed_by": "apply_lease_agent", "present": True,
         "data": [{"port": "Ethernet6/1", "ou": "6L"}]},
    ])
    monkeypatch.setattr(config_view, "site_env_switches", lambda: [
        {"key": "MGMT_VIP", "value": "10.0.0.1"},
    ])
    monkeypatch.setattr(config_view, "credentials_reference", lambda: [
        {"path": "/etc/flax/credentials.json",
         "description": "Switch CLI credentials",
         "key_names": ["eosuser", "eospass"],
         "note": "vault-managed — edit via ansible-vault; values never surfaced here."},
    ])
    r = client.get("/config")
    assert r.status_code == 200
    assert b"Config" in r.content
    assert b"sweep_interval_secs" in r.content


def test_config_page_shows_credential_key_names(client, monkeypatch):
    """The /config page lists each credential file's key names and its vault note."""
    from flax_control import config_view, queries
    monkeypatch.setattr(queries, "consumer_acks", lambda: [])
    monkeypatch.setattr(config_view, "reconcile_tunables", lambda: [])
    monkeypatch.setattr(config_view, "topology_files", lambda: [])
    monkeypatch.setattr(config_view, "site_env_switches", lambda: [])
    monkeypatch.setattr(config_view, "credentials_reference", lambda: [
        {"path": "/etc/flax/credentials.json",
         "description": "Switch CLI creds",
         "key_names": ["eosuser", "eospass", "cisco_user", "cisco_pass"],
         "note": "vault-managed — edit via ansible-vault; values never surfaced here."},
    ])
    r = client.get("/config")
    assert r.status_code == 200
    for key in (b"eosuser", b"eospass", b"cisco_user", b"cisco_pass"):
        assert key in r.content
    assert b"/etc/flax/credentials.json" in r.content


def test_config_page_shows_missing_file_note(client, monkeypatch):
    """Files marked present=False show 'not present on this host'."""
    from flax_control import config_view, queries
    monkeypatch.setattr(queries, "consumer_acks", lambda: [])
    monkeypatch.setattr(config_view, "reconcile_tunables", lambda: [])
    monkeypatch.setattr(config_view, "topology_files", lambda: [
        {"path": "/etc/flax/switches.json", "drives": "switch inventory",
         "deployed_by": "apply_lease_agent", "present": False, "data": None},
    ])
    monkeypatch.setattr(config_view, "site_env_switches", lambda: [])
    monkeypatch.setattr(config_view, "credentials_reference", lambda: [])
    r = client.get("/config")
    assert r.status_code == 200
    assert b"not present" in r.content


def test_config_page_has_registry_and_materializer_drilldown(client, monkeypatch):
    """The /config page links out to the folded /roles and /shadow routes
    (Registry + Materializer) so those drill-downs stay reachable."""
    from flax_control import config_view, queries
    monkeypatch.setattr(queries, "consumer_acks", lambda: [])
    monkeypatch.setattr(config_view, "reconcile_tunables", lambda: [])
    monkeypatch.setattr(config_view, "topology_files", lambda: [])
    monkeypatch.setattr(config_view, "classifier_dirs", lambda: [])
    monkeypatch.setattr(config_view, "site_env_switches", lambda: [])
    monkeypatch.setattr(config_view, "credentials_reference", lambda: [])
    monkeypatch.setattr(config_view, "bmc_fw_manifest", lambda: {})
    r = client.get("/config")
    assert r.status_code == 200
    assert b'href="/roles"' in r.content
    assert b'href="/shadow"' in r.content


def test_services_page_has_folded_route_drilldowns(client):
    """The /services page links out to all folded routes (Registry, Materializer,
    lanes, ownership, BMC-FW, events) so those drill-downs stay reachable."""
    from flax_control import queries
    from unittest.mock import patch
    with patch.object(queries, "consumer_acks", return_value=[]):
        r = client.get("/services")
    assert r.status_code == 200
    for href in ('href="/roles"', 'href="/shadow"', 'href="/lanes"',
                 'href="/ownership"', 'href="/bmc-fw"', 'href="/events"'):
        assert href in r.text, f"missing drill-down: {href}"


def test_base_template_has_config_nav_link(client, monkeypatch):
    """Any page carries the /config nav link."""
    from flax_control import queries
    monkeypatch.setattr(queries, "devices", lambda: [])
    r = client.get("/devices")
    assert r.status_code == 200
    assert b"/config" in r.content


# ---- /roles route tests --------------------------------------------------

def test_roles_page_renders_empty_registry(client, monkeypatch):
    """Empty roles/role_universe tables (not yet published) render the empty
    state, not a 500."""
    from flax_control import queries
    monkeypatch.setattr(queries, "roles", lambda: [])
    monkeypatch.setattr(queries, "role_universe", lambda: [])
    monkeypatch.setattr(queries, "switch_ports_all", lambda: [])
    r = client.get("/roles")
    assert r.status_code == 200
    assert b"Roles" in r.content
    assert b"No roles published yet" in r.content


def test_roles_page_renders_populated_registry_and_coverage(client, monkeypatch):
    """A populated registry shows role rows + catch-all pill + uncovered ports."""
    from flax_control import queries
    monkeypatch.setattr(queries, "roles", lambda: [
        ("triage", {"role": "triage", "capabilities": {"reconcile_switch": True},
                     "policy": {"vid": {"default": 16}},
                     "record_keys": ["mac", "serial"]}, 3, "2026-07-03T12:00:00"),
        ("post", {"role": "post", "capabilities": {"reconcile_switch": False},
                   "policy": {"vid": {"default": 24}},
                   "record_keys": ["customer"]}, 3, "2026-07-03T12:00:00"),
    ])
    monkeypatch.setattr(queries, "role_universe", lambda: [
        ("triage", "port", "*geometry*", "et6/1"),
        ("post", "switch", "rabbit-edam", None),
        ("post", "catch_all", None, None),
    ])
    monkeypatch.setattr(queries, "switch_ports_all", lambda: [
        ("rabbit-gouda", "Ethernet6/1"),
        ("hare-1", "Ethernet2/1"),
    ])
    r = client.get("/roles")
    assert r.status_code == 200
    assert b"triage" in r.content
    assert b"post" in r.content
    assert b"catch-all" in r.content
    # hare-1 is not explicitly claimed -> shows in the coverage section
    assert b"hare-1" in r.content


# ---- /shadow route tests ---------------------------------------------------

def test_shadow_page_renders_empty_tables(client, monkeypatch):
    """Empty materializer_plan/mac_ownership_events/desired_reservations
    render the "never ran" banner + empty states, not a 500. An empty
    materializer_plan table is unambiguous now that every run writes a
    summary marker row -- it means the materializer has never run, not
    "converged forever"."""
    from flax_control import queries
    monkeypatch.setattr(queries, "materializer_recent", lambda limit=500: [])
    monkeypatch.setattr(queries, "ownership_events_recent", lambda limit=200: [])
    monkeypatch.setattr(queries, "desired_summary", lambda: [])
    r = client.get("/shadow")
    assert r.status_code == 200
    assert b"Shadow" in r.content
    assert b"never ran" in r.content
    assert b"No plan rows recorded" in r.content
    assert b"No ownership events recorded yet" in r.content
    assert b"No desired reservations published yet" in r.content


def test_shadow_page_renders_non_converged_run(client, monkeypatch):
    """A populated materializer_plan shows the not-converged banner and the
    per-owner/action counts + full plan history."""
    from flax_control import queries
    monkeypatch.setattr(queries, "materializer_recent", lambda limit=500: [
        ("2026-07-04T10:10:00", "triage", "upsert", "aa:bb:cc:00:00:01",
         {"run": 2, "fields": ["hostname"]}),
        ("2026-07-04T10:10:00", "post", "delete", "aa:bb:cc:00:00:02", {"run": 2}),
    ])
    monkeypatch.setattr(queries, "ownership_events_recent", lambda limit=200: [
        ("2026-07-04T10:09:00", "aa:bb:cc:00:00:03", None, "triage",
         "rabbit-gouda", "et6/1"),
    ])
    monkeypatch.setattr(queries, "desired_summary", lambda: [
        ("triage", 12, "2026-07-04T10:08:00"),
    ])
    r = client.get("/shadow")
    assert r.status_code == 200
    assert b"not converged" in r.content
    assert b"aa:bb:cc:00:00:01" in r.content
    assert b"aa:bb:cc:00:00:03" in r.content
    assert b"triage" in r.content


def test_shadow_page_renders_converged_with_skips_as_amber(client, monkeypatch):
    """A latest run that planned 0 actions but skipped macs from planning
    must NOT render as a clean green convergence -- the template picks the
    amber "warn" pill and the skip-count banner text."""
    from flax_control import queries
    monkeypatch.setattr(queries, "materializer_recent", lambda limit=500: [
        ("2026-07-04T10:10:00", "-", "summary", "-",
         {"run": "abc:9", "planned": 0, "by_action": {},
          "skipped": {"unowned": 2, "unregistered_desired": 0, "multi_actual": 0}}),
    ])
    monkeypatch.setattr(queries, "ownership_events_recent", lambda limit=200: [])
    monkeypatch.setattr(queries, "desired_summary", lambda: [])
    r = client.get("/shadow")
    assert r.status_code == 200
    # Task 7 (design-system migration) swapped the old `.pill` markup for the
    # shared `c.badge()` macro -- amber/warn is now `class="badge w"` and a
    # clean-green convergence would be `class="badge g"` (never reached here).
    assert b'class="badge w"' in r.content
    assert b"skipped 2 macs from planning" in r.content
    assert b'class="badge g"' not in r.content


# ---- /switches/<sw> overview route tests --------------------------------

def test_switch_overview_renders_200(client, monkeypatch):
    """GET /switches/rabbit-lorax returns 200 with ports table."""
    from flax_control import queries
    monkeypatch.setattr(queries, "ports_for_switch", lambda sw: [
        {"port": "Ethernet6/1", "link": "link", "mask": None, "macs": []},
        {"port": "Ethernet6/2", "link": "notconnect", "mask": None, "macs": []},
    ])
    monkeypatch.setattr(queries, "desired_ports_for_switch", lambda sw: {})
    monkeypatch.setattr(queries, "switches", lambda: [
        {"switch": "rabbit-lorax", "driver": "eos", "polled_at": "2026-06-17T00:00:00",
         "reachable": True, "generation": 5, "port_count": 2},
    ])
    r = client.get("/switches/rabbit-lorax")
    assert r.status_code == 200
    assert b"rabbit-lorax" in r.content
    assert b"Ethernet6/1" in r.content


def test_switch_overview_unknown_switch_still_renders(client, monkeypatch):
    """Unknown switch (no switch_facts row) renders 200 with empty ports."""
    from flax_control import queries
    monkeypatch.setattr(queries, "ports_for_switch", lambda sw: [])
    monkeypatch.setattr(queries, "desired_ports_for_switch", lambda sw: {})
    monkeypatch.setattr(queries, "switches", lambda: [])
    r = client.get("/switches/nonexistent-switch")
    assert r.status_code == 200
    assert b"nonexistent-switch" in r.content


def test_switches_page_links_to_overview(client, monkeypatch):
    """The /switches page links to /switches/<sw> (no /port suffix)."""
    from flax_control import queries
    monkeypatch.setattr(queries, "switches", lambda: [
        {"switch": "rabbit-lorax", "driver": "eos", "polled_at": None,
         "reachable": True, "generation": 1, "port_count": 48},
    ])
    r = client.get("/switches")
    assert r.status_code == 200
    assert b'href="/switches/rabbit-lorax"' in r.content


# --- delete reservation (hard delete of an orphaned kea.hosts row) ---

def test_post_delete_reservation_redirects(client, monkeypatch):
    """Form POST deletes the row and 303s back to /reservations."""
    from flax_control import reservations_delete
    calls = []
    monkeypatch.setattr(reservations_delete, "delete_reservation",
                        lambda mac: calls.append(mac) or True)
    r = client.post("/reservations/1c34da7fb3a4/delete",
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/reservations"
    assert calls == ["1c34da7fb3a4"]


def test_post_delete_reservation_bad_mac_returns_404(client, monkeypatch):
    from flax_control import reservations_delete

    def raise_notfound(mac):
        raise reservations_delete.NotFound(mac)
    monkeypatch.setattr(reservations_delete, "delete_reservation", raise_notfound)
    r = client.post("/reservations/deadbeefdead/delete", follow_redirects=False)
    assert r.status_code == 404


def test_reservations_html_has_delete_button(client, monkeypatch):
    """Each reservation row exposes a delete form posting to .../delete."""
    from flax_control import queries
    monkeypatch.setattr(queries, "reservations", lambda: [{
        "mac_hex": "1c34da7fb3a4", "mac": "1c:34:da:7f:b3:a4",
        "switch": "rabbit-lorax", "port": "Ethernet6/1", "kind": "bmc",
        "vid": 17, "ipv4_address": "172.17.6.1", "ipv6_address": None,
        "hostname": "tiogapass6b1-bmc", "subnet_id": 17,
        "operator_note": None, "aliases": None, "generation": 1,
        "updated_at": None,
    }])
    r = client.get("/reservations")
    assert r.status_code == 200
    assert b"/reservations/1c34da7fb3a4/delete" in r.content


# --- DNS aliases ---

def test_post_aliases_redirects(client, monkeypatch):
    from flax_control import aliases
    calls = []
    monkeypatch.setattr(aliases, "update_aliases",
                        lambda mac, text: calls.append((mac, text)) or text.split())
    r = client.post("/reservations/1c34da7fb3a4/aliases",
                    data={"aliases": "leo-mgmt extra1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/reservations"
    assert calls == [("1c34da7fb3a4", "leo-mgmt extra1")]


def test_post_aliases_invalid_returns_400(client, monkeypatch):
    from flax_control import aliases

    def raise_invalid(mac, text):
        raise aliases.InvalidAlias("bad_one")
    monkeypatch.setattr(aliases, "update_aliases", raise_invalid)
    r = client.post("/reservations/1c34da7fb3a4/aliases",
                    data={"aliases": "bad_one"}, follow_redirects=False)
    assert r.status_code == 400


def test_post_aliases_missing_mac_returns_404(client, monkeypatch):
    from flax_control import aliases

    def raise_notfound(mac, text):
        raise aliases.NotFound(mac)
    monkeypatch.setattr(aliases, "update_aliases", raise_notfound)
    r = client.post("/reservations/deadbeefdead/aliases",
                    data={"aliases": "x"}, follow_redirects=False)
    assert r.status_code == 404


def test_reservations_html_has_aliases_input(client, monkeypatch):
    from flax_control import queries
    monkeypatch.setattr(queries, "reservations", lambda: [{
        "mac_hex": "1c34da7fb3a4", "mac": "1c:34:da:7f:b3:a4",
        "switch": "rabbit-lorax", "port": "Ethernet6/1", "kind": "bmc",
        "vid": 17, "ipv4_address": "172.17.6.1", "ipv6_address": None,
        "hostname": "tiogapass6b1-bmc", "subnet_id": 17,
        "operator_note": None, "aliases": "leo-mgmt extra1", "generation": 1,
        "updated_at": None,
    }])
    r = client.get("/reservations")
    body = r.content.decode()
    assert "/reservations/1c34da7fb3a4/aliases" in body
    assert "leo-mgmt extra1" in body   # current aliases pre-filled in the input


# --- triage page ---

def test_triage_page_renders_geometry_ports_with_colored_states(client, monkeypatch):
    import flax_control.app as appmod
    from flax_control import queries
    monkeypatch.setattr(appmod, "_load_geometry",
                        lambda: [{"port": "et6b1", "ou": "20L", "switch": "rabbit-lorax"}])
    monkeypatch.setattr(queries, "observe_state_all", lambda: {
        "rabbit-lorax:Ethernet6/1": {
            "switch": "rabbit-lorax", "port": "Ethernet6/1",
            "vars": {"linkstate": {"value": "link", "since": "2026-06-19T00:00:00Z"},
                     "bmcping": {"value": "fail", "since": "2026-06-19T00:00:00Z"}},
            "last_polled": "2026-06-19T12:00:00Z", "resolved": {}}})
    r = client.get("/triage")
    assert r.status_code == 200
    body = r.content.decode()
    assert "Et6/1" in body and "20L" in body
    assert "st-good" in body and "st-bad" in body      # colored state cells
    assert "/switches/rabbit-lorax/Ethernet6/1" in body  # port drill-down link


def test_api_port_scoped_to_geometry_switch(client, monkeypatch):
    """Legacy switchportrecond-compat /api/v1/port must return the TRIAGE
    (geometry) switch's row, never a post switch that happens to share the port
    number. observe_state is cross-role now (observe enrolls rabbit-edam for
    post discovery), so a switch-blind 'take the first port match' leaks the
    post device onto the triage tile -- this pins the geometry switch."""
    import flax_control.app as appmod
    from flax_control import queries
    monkeypatch.setattr(appmod, "_load_geometry",
                        lambda: [{"port": "et10b1", "ou": "20L", "switch": "rabbit-gouda"}])
    monkeypatch.setattr(queries, "observe_state_all", lambda: {
        # post switch FIRST so a switch-blind matching[0] would (wrongly) pick it
        "rabbit-edam:et10b1": {"switch": "rabbit-edam", "port": "et10b1",
                               "vars": {}, "last_polled": None,
                               "resolved": {"bmc_mac": "98:03:9b:a8:25:72"}},
        "rabbit-gouda:et10b1": {"switch": "rabbit-gouda", "port": "et10b1",
                                "vars": {}, "last_polled": None,
                                "resolved": {"bmc_mac": "98:03:9b:a7:03:2c"}}})
    r = client.get("/api/v1/port/et10b1")
    assert r.status_code == 200
    assert r.json()["mac"] == "98:03:9b:a7:03:2c"   # rabbit-gouda (triage), NOT rabbit-edam


def test_api_ports_excludes_post_switch_geometry_portnumbers(client, monkeypatch):
    """/api/v1/ports must list a rack port only when a GEOMETRY (switch,port)
    row exists -- a post-switch row sharing the number must not add it."""
    import flax_control.app as appmod
    from flax_control import queries
    monkeypatch.setattr(appmod, "_load_geometry",
                        lambda: [{"port": "et6b1", "ou": "20L", "switch": "rabbit-gouda"}])
    monkeypatch.setattr(queries, "observe_state_all", lambda: {
        # only a POST switch has this geometry port number -> must NOT be listed
        "rabbit-edam:et6b1": {"switch": "rabbit-edam", "port": "et6b1",
                              "vars": {}, "last_polled": None, "resolved": {}}})
    r = client.get("/api/v1/ports")
    assert r.status_code == 200
    assert r.json() == []   # no rabbit-gouda row -> the triage rack has nothing here


def test_triage_page_excludes_non_geometry_ports(client, monkeypatch):
    import flax_control.app as appmod
    from flax_control import queries
    monkeypatch.setattr(appmod, "_load_geometry",
                        lambda: [{"port": "et6b1", "ou": "20L", "switch": "rabbit-lorax"}])
    monkeypatch.setattr(queries, "observe_state_all", lambda: {
        "turtle-lorax:swp9": {"switch": "turtle-lorax", "port": "swp9",
                              "vars": {}, "last_polled": None, "resolved": {}}})
    r = client.get("/triage")
    assert r.status_code == 200
    assert b"swp9" not in r.content        # turtle/post port not shown
    assert b"turtle-lorax" not in r.content


def test_base_template_has_triage_nav_link(client, monkeypatch):
    from flax_control import queries
    monkeypatch.setattr(queries, "devices", lambda: [])
    r = client.get("/devices")
    assert b'href="/triage"' in r.content
