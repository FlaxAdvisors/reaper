"""Tests for flax_control reconcile_requests enqueue helper + new queries.

Convention: mirrors test_flax_control_queries.py and test_flax_control_operator_notes.py
— MagicMock pool injected via monkeypatch.setattr on the module-level get_pool.
No real Postgres required; all tests run offline.
"""
from unittest.mock import MagicMock, call
import psycopg


# ---------------------------------------------------------------------------
# reconcile_requests.enqueue_flap
# ---------------------------------------------------------------------------

def _mock_pool_insert_ok():
    """Pool that accepts an INSERT without raising."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn


def _mock_pool_unique_violation():
    """Pool whose connection.execute raises UniqueViolation."""
    conn = MagicMock()
    conn.execute.side_effect = psycopg.errors.UniqueViolation("dup")
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn


def test_enqueue_flap_inserts_operator_request(monkeypatch):
    from flax_control import reconcile_requests

    pool, conn = _mock_pool_insert_ok()
    monkeypatch.setattr(reconcile_requests, "get_pool", lambda: pool)

    result = reconcile_requests.enqueue_flap(
        mac="aa:bb:cc:00:00:01", switch="s", port="p",
        kind="bmc", operator="dbahi")

    assert result is True
    assert conn.execute.call_count == 1
    sql, params = conn.execute.call_args[0]
    assert "INSERT INTO reconcile_requests" in sql
    assert params[0] == "op:dbahi"
    assert params[1] == "aa:bb:cc:00:00:01"
    assert params[4] == "bmc"
    assert params[5] == "operator flap"


def test_enqueue_flap_requested_by_prefixed_op(monkeypatch):
    """requested_by is always 'op:' + operator."""
    from flax_control import reconcile_requests

    pool, conn = _mock_pool_insert_ok()
    monkeypatch.setattr(reconcile_requests, "get_pool", lambda: pool)

    reconcile_requests.enqueue_flap(
        mac="aa:bb:cc:00:00:02", switch="s", port="p",
        kind=None, operator="alice")

    _, params = conn.execute.call_args[0]
    assert params[0] == "op:alice"
    assert params[4] is None  # kind=None passes through


def test_enqueue_flap_returns_false_on_duplicate_open_request(monkeypatch):
    """UniqueViolation from the open-mac partial index -> return False."""
    from flax_control import reconcile_requests

    pool, conn = _mock_pool_unique_violation()
    monkeypatch.setattr(reconcile_requests, "get_pool", lambda: pool)

    result = reconcile_requests.enqueue_flap(
        mac="aa:bb:cc:00:00:01", switch="s", port="p",
        kind="bmc", operator="dbahi")

    assert result is False


# ---------------------------------------------------------------------------
# reconcile_requests.enqueue_bmc_reset
# ---------------------------------------------------------------------------

def test_enqueue_bmc_reset_inserts_operator_request(monkeypatch):
    from flax_control import reconcile_requests

    pool, conn = _mock_pool_insert_ok()
    monkeypatch.setattr(reconcile_requests, "get_pool", lambda: pool)

    result = reconcile_requests.enqueue_bmc_reset(
        mac="aa:bb:cc:00:00:01", switch="s", port="p",
        kind="bmc", operator="dbahi")

    assert result is True
    assert conn.execute.call_count == 1
    sql, params = conn.execute.call_args[0]
    assert "INSERT INTO reconcile_requests" in sql
    assert params[0] == "op:dbahi"
    assert params[1] == "aa:bb:cc:00:00:01"
    assert params[4] == "bmc"
    assert params[5] == "operator bmc-reset"


def test_enqueue_bmc_reset_returns_false_on_duplicate(monkeypatch):
    from flax_control import reconcile_requests

    pool, conn = _mock_pool_unique_violation()
    monkeypatch.setattr(reconcile_requests, "get_pool", lambda: pool)

    result = reconcile_requests.enqueue_bmc_reset(
        mac="aa:bb:cc:00:00:01", switch="s", port="p",
        kind="bmc", operator="dbahi")

    assert result is False


# ---------------------------------------------------------------------------
# queries.reconcile_actions_for_port
# ---------------------------------------------------------------------------

def _mock_cursor_returning(rows):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    return cursor


def _mock_pool_with_cursor(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn


def test_reconcile_actions_for_port_returns_list_of_dicts(monkeypatch):
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 12, 0, 0)
    cursor = _mock_cursor_returning([
        (ts, "switch_flap", "success", "mismatch", {"mac": "aa:bb:cc:00:00:01"}),
    ])
    pool, conn = _mock_pool_with_cursor(cursor)
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    rows = queries.reconcile_actions_for_port("s", "p", limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "switch_flap"
    assert row["outcome"] == "success"
    assert row["reason"] == "mismatch"
    assert row["detail"] == {"mac": "aa:bb:cc:00:00:01"}
    assert row["ts"] == ts.isoformat()


def test_reconcile_actions_for_port_returns_empty_list_when_no_rows(monkeypatch):
    from flax_control import queries

    cursor = _mock_cursor_returning([])
    pool, _ = _mock_pool_with_cursor(cursor)
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    rows = queries.reconcile_actions_for_port("nosuchswitch", "nosuchport")
    assert rows == []


def test_reconcile_actions_for_port_passes_switch_port_limit_to_sql(monkeypatch):
    from flax_control import queries

    cursor = _mock_cursor_returning([])
    pool, _ = _mock_pool_with_cursor(cursor)
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    queries.reconcile_actions_for_port("rabbit-fiesta", "Ethernet6/1", limit=5)

    sql, params = cursor.execute.call_args[0]
    assert "reconcile_actions" in sql
    assert "switch" in sql and "port" in sql
    assert params["switch"] == "rabbit-fiesta"
    assert params["port"] == "Ethernet6/1"
    assert params["limit"] == 5


# ---------------------------------------------------------------------------
# queries.device_one
# ---------------------------------------------------------------------------

def _mock_pool_conn_execute(fetchone_return):
    """Pool where conn.execute(sql, params).fetchone() returns fetchone_return."""
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    conn = MagicMock()
    conn.execute.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn, cursor


def test_device_one_returns_dict_for_known_mac(monkeypatch):
    from flax_control import queries

    pool, conn, cursor = _mock_pool_conn_execute(
        ("aa:bb:cc:00:00:01", "rabbit-fiesta", "Ethernet6/1", "bmc", {"chassis": "WTH123"}))
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    result = queries.device_one("aa:bb:cc:00:00:01")

    assert result is not None
    assert result["mac"] == "aa:bb:cc:00:00:01"
    assert result["switch"] == "rabbit-fiesta"
    assert result["port"] == "Ethernet6/1"
    assert result["kind"] == "bmc"
    assert result["latched"] == {"chassis": "WTH123"}


def test_device_one_returns_none_for_unknown_mac(monkeypatch):
    from flax_control import queries

    pool, conn, cursor = _mock_pool_conn_execute(None)
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    result = queries.device_one("ff:ff:ff:ff:ff:ff")
    assert result is None


def test_device_one_uses_lower_mac_comparison(monkeypatch):
    """SQL must use lower(mac)=lower(%(mac)s) so casing is normalised."""
    from flax_control import queries

    pool, conn, cursor = _mock_pool_conn_execute(None)
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    queries.device_one("AA:BB:CC:00:00:01")

    sql, params = conn.execute.call_args[0]
    assert "lower" in sql.lower()
    assert params["mac"] == "AA:BB:CC:00:00:01"


def test_device_one_null_latched_becomes_empty_dict(monkeypatch):
    """latched=None in the DB should be returned as {} not None."""
    from flax_control import queries

    pool, conn, cursor = _mock_pool_conn_execute(
        ("aa:bb:cc:00:00:01", "s", "p", "host", None))
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    result = queries.device_one("aa:bb:cc:00:00:01")
    assert result["latched"] == {}


# ---------------------------------------------------------------------------
# Task D2: POST /reconcile/flap route + GET /devices/{mac} route
# ---------------------------------------------------------------------------

def _mock_pool_for_route():
    """Pool that accepts an INSERT without raising, for FastAPI TestClient tests."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn


def test_post_flap_enqueues_and_redirects(monkeypatch):
    """POST /reconcile/flap enqueues a request and returns 303."""
    pool, conn = _mock_pool_for_route()
    import flax_control.reconcile_requests as rr
    import flax_control.db as db_mod
    monkeypatch.setattr(rr, "get_pool", lambda: pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)

    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/reconcile/flap",
        data={"mac": "aa:bb:cc:00:00:01", "switch": "s",
              "port": "p", "kind": "bmc",
              "return_to": "/switches/s/p"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/switches/s/p"
    # enqueue_flap should have called conn.execute once (INSERT)
    assert conn.execute.call_count == 1
    sql, params = conn.execute.call_args[0]
    assert "INSERT INTO reconcile_requests" in sql
    assert params[1] == "aa:bb:cc:00:00:01"


def test_post_flap_canonicalizes_internal_port_to_arista(monkeypatch):
    """The device page passes devices.port in internal form (et6b1); the POST
    handler must enqueue the Arista form (Ethernet6/1) so flax-reconcile's flap
    + sentinel are valid (spec §6)."""
    pool, conn = _mock_pool_for_route()
    import flax_control.reconcile_requests as rr
    import flax_control.db as db_mod
    monkeypatch.setattr(rr, "get_pool", lambda: pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)

    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/reconcile/flap",
        data={"mac": "aa:bb:cc:00:00:09", "switch": "rabbit-x",
              "port": "et6b1", "kind": "bmc"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    sql, params = conn.execute.call_args[0]
    assert "INSERT INTO reconcile_requests" in sql
    # params order: (requested_by, mac, switch, port, kind, reason)
    assert params[3] == "Ethernet6/1", (
        "POST handler must canonicalize et6b1 -> Ethernet6/1, got " + str(params[3]))


def test_post_flap_does_not_double_convert_arista_port(monkeypatch):
    """The switch_detail page already passes the Arista URL form; arista_port is
    idempotent so it stays Ethernet6/1 (no double-conversion)."""
    pool, conn = _mock_pool_for_route()
    import flax_control.reconcile_requests as rr
    import flax_control.db as db_mod
    monkeypatch.setattr(rr, "get_pool", lambda: pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)

    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/reconcile/flap",
        data={"mac": "aa:bb:cc:00:00:0a", "switch": "rabbit-x",
              "port": "Ethernet6/1", "kind": "bmc"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    _sql, params = conn.execute.call_args[0]
    assert params[3] == "Ethernet6/1"


def test_post_flap_redirects_to_root_when_no_return_to(monkeypatch):
    """return_to defaults to '/'."""
    pool, conn = _mock_pool_for_route()
    import flax_control.reconcile_requests as rr
    import flax_control.db as db_mod
    monkeypatch.setattr(rr, "get_pool", lambda: pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)

    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/reconcile/flap",
        data={"mac": "aa:bb:cc:00:00:02", "switch": "s", "port": "p"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_post_bmc_reset_enqueues_and_redirects(monkeypatch):
    """POST /reconcile/bmc-reset enqueues reason='operator bmc-reset' and 303s."""
    pool, conn = _mock_pool_for_route()
    import flax_control.reconcile_requests as rr
    import flax_control.db as db_mod
    monkeypatch.setattr(rr, "get_pool", lambda: pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)

    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/reconcile/bmc-reset",
        data={"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-x",
              "port": "et6b1", "kind": "bmc",
              "return_to": "/devices/aa:bb:cc:00:00:01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/devices/aa:bb:cc:00:00:01"
    assert conn.execute.call_count == 1
    sql, params = conn.execute.call_args[0]
    assert "INSERT INTO reconcile_requests" in sql
    assert params[1] == "aa:bb:cc:00:00:01"
    # port canonicalized et6b1 -> Ethernet6/1 (mirror flap path)
    assert params[3] == "Ethernet6/1"
    assert params[5] == "operator bmc-reset"


def test_post_bmc_reset_defaults_return_to_root(monkeypatch):
    pool, conn = _mock_pool_for_route()
    import flax_control.reconcile_requests as rr
    import flax_control.db as db_mod
    monkeypatch.setattr(rr, "get_pool", lambda: pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)

    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/reconcile/bmc-reset",
        data={"mac": "aa:bb:cc:00:00:02", "switch": "s", "port": "p"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


# ---------------------------------------------------------------------------
# device_detail.html: stuck-BMC banner + Reset BMC button
# ---------------------------------------------------------------------------

def _device_full_with_observe(bmcipmi, bmcping):
    """Minimal device_full() shape exercising the banner condition."""
    return {
        "mac": "aa:bb:cc:00:00:01", "switch": "rabbit-x", "port": "et6b1",
        "kind": "bmc", "latched": {}, "last_seen": None, "reservation": None,
        "observe": {
            "vars": {"bmcipmi": {"value": bmcipmi, "since": None},
                     "bmcping": {"value": bmcping, "since": None}},
            "last_polled": None, "generation": 1, "resolved": {},
        },
        "port_facts": None, "desired": None,
        "last_reconcile_action": None, "recent_events": [],
    }


def _patch_device_full(monkeypatch, full):
    import flax_control.queries as queries_mod
    import flax_control.db as db_mod
    pool, _ = _mock_pool_for_route()
    monkeypatch.setattr(queries_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(queries_mod, "device_full", lambda mac: full)
    monkeypatch.setattr(queries_mod, "reconcile_actions_for_port",
                        lambda *a, **k: [])


def test_device_page_shows_stuck_banner_and_reset_button(monkeypatch):
    """bmcipmi=unknown & bmcping=ok -> warning banner + Reset BMC button."""
    _patch_device_full(monkeypatch,
                       _device_full_with_observe("unknown", "ok"))
    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    assert "session-exhausted" in resp.text
    assert 'action="/reconcile/bmc-reset"' in resp.text


def test_device_page_no_banner_when_ipmi_ok(monkeypatch):
    """bmcipmi present (not unknown) -> no banner, but reset button still there."""
    _patch_device_full(monkeypatch,
                       _device_full_with_observe("openbmc", "ok"))
    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/devices/aa:bb:cc:00:00:01")
    assert resp.status_code == 200
    assert "session-exhausted" not in resp.text
    # Reset button is always present when a device exists (like the flap button)
    assert 'action="/reconcile/bmc-reset"' in resp.text


def test_get_devices_mac_not_found_renders_page(monkeypatch):
    """GET /devices/{mac} when device not found still renders (device=None, actions=[])."""
    pool, conn = _mock_pool_for_route()
    # device_one returns None (fetchone returns None)
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn.execute.return_value = cursor

    import flax_control.queries as queries_mod
    import flax_control.db as db_mod
    monkeypatch.setattr(queries_mod, "get_pool", lambda: pool)
    monkeypatch.setattr(db_mod, "get_pool", lambda: pool)

    from fastapi.testclient import TestClient
    from flax_control.app import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.get("/devices/ff:ff:ff:ff:ff:ff")
    assert resp.status_code == 200
    assert "ff:ff:ff:ff:ff:ff" in resp.text
