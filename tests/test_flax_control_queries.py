"""Tests for flax_control.queries — SQL backing the new views."""
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# _colon_mac / _mac_hex — MAC normalisation helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1c34da7f9d32", "1c:34:da:7f:9d:32"),
    ("1C34DA7F9D32", "1c:34:da:7f:9d:32"),
    ("1c:34:da:7f:9d:32", "1c:34:da:7f:9d:32"),
    ("1C:34:DA:7F:9D:32", "1c:34:da:7f:9d:32"),
    ("1c-34-da-7f-9d-32", "1c:34:da:7f:9d:32"),
    ("1c34.da7f.9d32", "1c:34:da:7f:9d:32"),
])
def test_colon_mac_normalises_any_form(raw, expected):
    from flax_control import queries
    assert queries._colon_mac(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1C:34:DA:7F:9D:32", "1c34da7f9d32"),
    ("1c-34-da-7f-9d-32", "1c34da7f9d32"),
    ("1c34da7f9d32", "1c34da7f9d32"),
])
def test_mac_hex_strips_separators(raw, expected):
    from flax_control import queries
    assert queries._mac_hex(raw) == expected


def test_switches_returns_list_of_dicts(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("rabbit-lorax", "eos", "2026-05-24T13:00:00Z", True, 412, 80),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.switches()
    assert len(out) == 1
    assert out[0]["switch"] == "rabbit-lorax"
    assert out[0]["driver"] == "eos"
    assert out[0]["reachable"] is True
    assert out[0]["port_count"] == 80


def test_ports_for_switch_returns_per_port_dicts(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("Ethernet6/1", {"link": "link", "macs": ["aa"],
                          "lldp_neighbors": [{"mac": "aa", "sysname": "x"}],
                          "bmc_mac": "aa", "nic_macs": ["bb"], "junk_macs": [],
                          "classification_source": "lldp", "lldp_disagreement": False,
                          "mask": "access"}),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.ports_for_switch("rabbit-lorax")
    assert out[0]["port"] == "Ethernet6/1"
    assert out[0]["link"] == "link"
    assert out[0]["bmc_mac"] == "aa"


def test_natural_port_key_orders_numerically_not_lexicographically():
    """et2b1 < Ethernet6/1 < et10b2 (numeric); swp3 < swp23; rabbit before turtle."""
    from flax_control import queries

    ports = ["et10b2", "et2b1", "swp23", "swp3", "Ethernet6/1"]
    out = [p["port"] for p in
           sorted(({"port": p} for p in ports),
                  key=lambda d: queries._natural_port_key(d["port"]))]
    assert out == ["et2b1", "Ethernet6/1", "et10b2", "swp3", "swp23"]


def test_natural_port_key_unknown_form_sorts_last_by_string():
    from flax_control import queries
    # Unknown tokens go to bucket 2, ordered by the raw string.
    assert queries._natural_port_key("zebra") == (2, 0, 0, "zebra")
    assert queries._natural_port_key("Ethernet6/1")[0] == 0
    assert queries._natural_port_key("swp7")[0] == 1


def test_ports_for_switch_sorts_naturally(monkeypatch):
    """ports_for_switch returns ports in natural (numeric) order regardless of
    the SQL row order: et2b1 before et10b2, Ethernet6/1 between them."""
    from flax_control import queries

    cursor = MagicMock()
    # Rows arrive in lexicographic order (as ORDER BY key would yield).
    cursor.fetchall.return_value = [
        ("Ethernet6/1", {"link": "link"}),
        ("et10b2", {"link": "down"}),
        ("et2b1", {"link": "link"}),
        ("swp23", {"link": "link"}),
        ("swp3", {"link": "link"}),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = [p["port"] for p in queries.ports_for_switch("rabbit-lorax")]
    assert out == ["et2b1", "Ethernet6/1", "et10b2", "swp3", "swp23"]


def test_observe_state_all_returns_keyed_by_switch_port(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("rabbit-lorax", "et6b1",
         {"linkstate": {"value": "link", "since": "..."}},
         "2026-05-24T13:00:00Z", 53,
         {"bmc_mac": "98:03:9b:a6:fc:24", "chassis_sn": "WTH123"}),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.observe_state_all()
    assert "rabbit-lorax:et6b1" in out
    row = out["rabbit-lorax:et6b1"]
    assert row["vars"]["linkstate"]["value"] == "link"
    assert row["resolved"]["bmc_mac"] == "98:03:9b:a6:fc:24"
    assert row["resolved"]["chassis_sn"] == "WTH123"
    assert row["generation"] == 53


def test_events_supports_filters(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (1, "2026-05-24T13:00:00Z", "flax-observe", "transition",
         None, "rabbit-lorax", "et6b1",
         {"var": "linkstate", "from": "unknown", "to": "link"}),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.events(service="flax-observe", limit=50)
    assert len(out) == 1
    assert out[0]["service"] == "flax-observe"
    assert out[0]["kind"] == "transition"
    # Verify the SQL got the right params
    sql, params = cursor.execute.call_args[0]
    assert "WHERE" in sql
    assert "service = %(service)s" in sql
    assert "LIMIT %(limit)s" in sql
    assert params["service"] == "flax-observe"
    assert params["limit"] == 50


def test_consumer_acks_returns_rows(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("flax-observe", "switch_facts", 412, "applied",
         "2026-05-24T13:00:00Z", None),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.consumer_acks()
    assert out[0]["consumer"] == "flax-observe"
    assert out[0]["source"] == "switch_facts"
    assert out[0]["action"] == "applied"


import datetime as _dt
from unittest.mock import MagicMock


def test_reservations_returns_list_of_dicts(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    # View rows: the trailing column is the new ipv6_address (migration 020).
    cursor.fetchall.return_value = [
        ("1c34da7fb3a4", "rabbit-lorax", "et6b1", "bmc", 17,
         "172.17.6.101", "leopard6b1-bmc", 17,
         None, 3, "2026-05-24T18:00:00+00:00", "fd00:17::6:101", "leo-mgmt extra1"),
        ("1c34da7fb3a2", "rabbit-lorax", "et6b1", "host", 17,
         "172.17.6.1",   "leopard6b1",     17,
         "flaky -- check IPMI weekly", 2, "2026-05-24T18:00:00+00:00", None, None),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.execute.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.reservations()
    assert len(out) == 2
    # MAC is now rendered colon-lowercase (consistent with the Leases page);
    # the bare-hex form is preserved as mac_hex for the operator-note form.
    by_mac = {r["mac"]: r for r in out}
    assert "1c:34:da:7f:b3:a4" in by_mac
    res = by_mac["1c:34:da:7f:b3:a4"]
    assert res["mac_hex"] == "1c34da7fb3a4"
    assert res["ipv4_address"] == "172.17.6.101"
    assert res["ipv6_address"] == "fd00:17::6:101"
    assert res["hostname"]     == "leopard6b1-bmc"
    assert res["operator_note"] is None
    assert res["aliases"] == "leo-mgmt extra1"
    assert by_mac["1c:34:da:7f:b3:a2"]["operator_note"] == "flaky -- check IPMI weekly"
    assert by_mac["1c:34:da:7f:b3:a2"]["ipv6_address"] is None
    assert by_mac["1c:34:da:7f:b3:a2"]["aliases"] is None
    assert res["updated_at"].startswith("2026-05-24T18:00")
    # SQL fragments
    sql = conn.execute.call_args[0][0]
    assert "FROM reservations" in sql
    assert "ORDER BY updated_at DESC" in sql
    assert "ipv6_address" in sql


def test_reservations_returns_empty_list_when_table_empty(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.execute.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)
    assert queries.reservations() == []


# ---------------------------------------------------------------------------
# queries.devices()
# ---------------------------------------------------------------------------

def test_devices_returns_mapped_rows(monkeypatch):
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 10, 0, 0)
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("aa:bb:cc:00:00:01", "rabbit-fiesta", "et6b1", "bmc", "tiogapass", ts),
        ("aa:bb:cc:00:00:02", "rabbit-fiesta", "et6b2", "host", None, ts),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    rows = queries.devices()
    assert len(rows) == 2
    assert rows[0]["mac"] == "aa:bb:cc:00:00:01"
    assert rows[0]["switch"] == "rabbit-fiesta"
    assert rows[0]["port"] == "et6b1"
    assert rows[0]["kind"] == "bmc"
    assert rows[0]["family"] == "tiogapass"
    assert rows[0]["last_seen"] == ts.isoformat()
    assert rows[1]["family"] is None


def test_devices_sql_selects_family_and_last_seen(monkeypatch):
    """SQL must project latched->>'family' AS family and last_seen."""
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    queries.devices()
    sql = cursor.execute.call_args[0][0]
    assert "latched->>'family'" in sql
    assert "last_seen" in sql
    assert "ORDER BY switch, port" in sql


def test_devices_returns_empty_list_when_no_rows(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    assert queries.devices() == []


# ---------------------------------------------------------------------------
# queries.device_full()
# ---------------------------------------------------------------------------

def _make_multi_query_pool(fetchone_sequence):
    """Return a pool whose conn.execute().fetchone() cycles through the sequence.

    Each call to conn.execute() returns a fresh MagicMock cursor whose
    .fetchone() returns the next value from fetchone_sequence.
    """
    from unittest.mock import MagicMock

    calls = iter(fetchone_sequence)

    def make_cursor():
        c = MagicMock()
        c.fetchone.return_value = next(calls)
        return c

    conn = MagicMock()
    conn.execute.side_effect = lambda *a, **kw: make_cursor()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False

    # cursor() context manager path (used by reconcile_actions + events queries).
    cursor_ctx = MagicMock()
    cursor_ctx.__enter__.return_value = cursor_ctx
    cursor_ctx.__exit__.return_value = False
    cursor_ctx.fetchall.return_value = []
    cursor_ctx.fetchone.return_value = None
    conn.cursor.return_value = cursor_ctx

    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


def test_device_full_returns_none_for_unknown_mac(monkeypatch):
    from flax_control import queries

    pool = _make_multi_query_pool([None])
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    assert queries.device_full("ff:ff:ff:ff:ff:ff") is None


def test_device_full_assembles_cross_service_dict(monkeypatch):
    """device_full returns all expected keys when every sub-query hits."""
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 10, 0, 0)
    import json

    # Sequence of fetchone() returns for each conn.execute() call:
    # 1. devices row
    # 2. kea.hosts row
    # 3. observe_state row
    # 4. switch_facts row (ports->> json)
    # 5. desired_port row
    fetchone_sequence = [
        # 1. devices
        ("aa:bb:cc:00:00:01", "rabbit-fiesta", "et6b1", "bmc",
         {"family": "tiogapass"}, ts),
        # 2. kea.hosts
        ("172.17.6.101", "leopard6b1-bmc", 17,
         json.dumps({"classify": {"vid": 17}, "operator_note": "ok"})),
        # 3. observe_state
        ({"linkstate": {"value": "link", "since": "t1"}},
         ts, 53, {"bmc_mac": "aa:bb:cc:00:00:01"}),
        # 4. switch_facts -> ports->>port_arista
        ({"link": "link", "macs": ["aa:bb:cc:00:00:01"],
          "bmc_mac": "aa:bb:cc:00:00:01", "nic_macs": [], "junk_macs": [],
          "classification_source": "lldp", "lldp_disagreement": False,
          "mask": "access", "lldp_neighbors": []},),
        # 5. desired_port
        (17, [{"mac": "aa:bb:cc:00:00:01", "kind": "bmc"}], 12),
    ]
    pool = _make_multi_query_pool(fetchone_sequence)
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    result = queries.device_full("aa:bb:cc:00:00:01")

    assert result is not None
    assert result["mac"] == "aa:bb:cc:00:00:01"
    assert result["switch"] == "rabbit-fiesta"
    assert result["port"] == "et6b1"
    assert result["kind"] == "bmc"
    assert result["last_seen"] == ts.isoformat()

    # Reservation
    assert result["reservation"] is not None
    assert result["reservation"]["ipv4_address"] == "172.17.6.101"
    assert result["reservation"]["hostname"] == "leopard6b1-bmc"
    assert result["reservation"]["classify"] == {"vid": 17}
    assert result["reservation"]["operator_note"] == "ok"

    # Observe
    assert result["observe"] is not None
    assert result["observe"]["generation"] == 53
    assert result["observe"]["resolved"]["bmc_mac"] == "aa:bb:cc:00:00:01"

    # port_facts
    assert result["port_facts"] is not None
    assert result["port_facts"]["link"] == "link"

    # desired
    assert result["desired"] is not None
    assert result["desired"]["desired_vid"] == 17

    # recent_events and last_reconcile_action come from cursor() (returns []/None in mock)
    assert result["recent_events"] == []
    assert result["last_reconcile_action"] is None


def test_device_full_none_reservation_when_kea_miss(monkeypatch):
    """If kea.hosts has no row for the MAC, reservation=None."""
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 10, 0, 0)
    fetchone_sequence = [
        ("aa:bb:cc:00:00:01", "rabbit-fiesta", "et6b1", "bmc", {}, ts),
        None,   # kea miss
        None,   # observe miss
        None,   # switch_facts miss
        None,   # desired_port miss
    ]
    pool = _make_multi_query_pool(fetchone_sequence)
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    result = queries.device_full("aa:bb:cc:00:00:01")
    assert result["reservation"] is None
    assert result["observe"] is None
    assert result["port_facts"] is None
    assert result["desired"] is None


def test_device_full_uses_arista_port_for_switch_facts_lookup(monkeypatch):
    """switch_facts lookup must use Arista form (Ethernet6/1), not internal (et6b1)."""
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 10, 0, 0)

    execute_calls = []

    class _CapturingConn:
        def execute(self, sql, params=None):
            execute_calls.append((sql, params))
            c = MagicMock()
            c.fetchone.return_value = None
            return c

        def cursor(self):
            curs = MagicMock()
            curs.__enter__ = lambda s: curs
            curs.__exit__ = lambda s, *a: False
            curs.fetchone.return_value = None
            curs.fetchall.return_value = []
            return curs

        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Pool:
        def connection(self):
            return _CapturingConn()

    # Override the first execute to return the device row.
    first = True

    class _SmartConn(_CapturingConn):
        def execute(self, sql, params=None):
            execute_calls.append((sql, params))
            c = MagicMock()
            if "FROM devices" in sql:
                c.fetchone.return_value = (
                    "aa:bb:cc:00:00:01", "rabbit-x", "et6b1", "bmc", {}, ts)
            else:
                c.fetchone.return_value = None
            return c

        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _SmartPool:
        def connection(self):
            return _SmartConn()

    monkeypatch.setattr(queries, "get_pool", lambda: _SmartPool())

    queries.device_full("aa:bb:cc:00:00:01")

    # Find the switch_facts call and assert its port param is Arista form.
    sf_calls = [(sql, p) for sql, p in execute_calls if "switch_facts" in sql]
    assert sf_calls, "No switch_facts query issued"
    sf_params = sf_calls[0][1]
    assert sf_params["port"] == "Ethernet6/1", (
        "switch_facts lookup must use Arista port form; got " + repr(sf_params.get("port")))


def test_device_full_kea_uses_stripped_mac_hex(monkeypatch):
    """kea.hosts lookup must use mac with colons stripped + lowercased."""
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 10, 0, 0)
    execute_calls = []

    class _SmartConn:
        def execute(self, sql, params=None):
            execute_calls.append((sql, params or {}))
            c = MagicMock()
            if "FROM devices" in sql:
                c.fetchone.return_value = (
                    "AA:BB:CC:00:11:22", "rabbit-x", "et6b1", "bmc", {}, ts)
            else:
                c.fetchone.return_value = None
            return c

        def cursor(self):
            curs = MagicMock()
            curs.__enter__ = lambda s: curs
            curs.__exit__ = lambda s, *a: False
            curs.fetchone.return_value = None
            curs.fetchall.return_value = []
            return curs

        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _SmartPool:
        def connection(self):
            return _SmartConn()

    monkeypatch.setattr(queries, "get_pool", lambda: _SmartPool())

    queries.device_full("AA:BB:CC:00:11:22")

    kea_calls = [(sql, p) for sql, p in execute_calls if "kea.hosts" in sql]
    assert kea_calls, "No kea.hosts query issued"
    hex_val = kea_calls[0][1].get("hex")
    assert hex_val == "aabbcc001122", (
        "kea.hosts lookup must use stripped+lowercased hex; got " + repr(hex_val))


# ---------------------------------------------------------------------------
# queries.leases()
# ---------------------------------------------------------------------------

def test_leases_returns_active_kea_leases_sorted_by_ip(monkeypatch):
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 12, 0, 0)
    cursor = MagicMock()
    # First execute() = lease4 rows (unsorted); second = lease6 (empty).
    cursor.fetchall.side_effect = [
        [("aa:bb:cc:00:00:02", "172.17.6.10", 17, ts, 0),
         ("aa:bb:cc:00:00:01", "172.17.6.2", 17, ts, 0)],
        [],  # lease6
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.leases()
    assert len(out) == 2
    # Sorted numerically by IP: .2 before .10
    assert out[0]["ip"] == "172.17.6.2"
    assert out[1]["ip"] == "172.17.6.10"
    assert out[0]["mac"] == "aa:bb:cc:00:00:01"
    assert out[0]["subnet_id"] == 17
    assert out[0]["state"] == 0
    assert out[0]["expires"] == ts.isoformat()


def test_leases_sql_targets_lease4_state0(monkeypatch):
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.side_effect = [[], []]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    queries.leases()
    sqls = [c.args[0] for c in cursor.execute.call_args_list]
    assert any("kea.lease4" in s for s in sqls)
    assert any("state = 0" in s for s in sqls)
    # MAC rendered via regexp_replace/encode, IP via host('0.0.0.0'::inet + address)
    assert any("host(('0.0.0.0'::inet) + address)" in s for s in sqls)


def test_leases_survives_missing_lease6_table(monkeypatch):
    """A site without kea.lease6 (or v6 leases) must still return v4 rows."""
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 12, 0, 0)
    cursor = MagicMock()

    def _exec(sql, *a, **kw):
        if "kea.lease6" in sql:
            raise RuntimeError("relation kea.lease6 does not exist")
        return None
    cursor.execute.side_effect = _exec
    cursor.fetchall.return_value = [("aa:bb:cc:00:00:01", "172.17.6.2", 17, ts, 0)]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.leases()
    assert len(out) == 1
    assert out[0]["ip"] == "172.17.6.2"


def test_leases_v6_duid_row_surfaces_duid_when_hwaddr_null(monkeypatch):
    """A v6 lease keyed by DUID has a NULL hwaddr -> rendered mac is NULL.
    queries.leases() must surface the DUID so the row isn't blank."""
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 12, 0, 0)
    cursor = MagicMock()
    # v4 (empty) then v6: (mac=NULL, ip, subnet, expire, state, duid_hex)
    cursor.fetchall.side_effect = [
        [],
        [(None, "fd00:17::6:1", 17, ts, 0, "00030001aabbccddeeff")],
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.leases()
    assert len(out) == 1
    row = out[0]
    assert row["ip"] == "fd00:17::6:1"
    # mac falls back to a duid:<hex> placeholder; raw duid also exposed.
    assert row["mac"] == "duid:00030001aabbccddeeff"
    assert row["duid"] == "00030001aabbccddeeff"


def test_leases_v6_sql_selects_duid(monkeypatch):
    """The v6 lease query must project encode(duid,'hex')."""
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchall.side_effect = [[], []]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    queries.leases()
    sqls = [c.args[0] for c in cursor.execute.call_args_list]
    assert any("kea.lease6" in s and "encode(duid, 'hex')" in s for s in sqls)


def test_leases_v4_row_keeps_real_mac_and_null_duid(monkeypatch):
    """v4 leases (hwaddr present) keep their MAC; duid is None (padded)."""
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 12, 0, 0)
    cursor = MagicMock()
    cursor.fetchall.side_effect = [
        [("aa:bb:cc:00:00:01", "172.17.6.2", 17, ts, 0)],
        [],
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    out = queries.leases()
    assert out[0]["mac"] == "aa:bb:cc:00:00:01"
    assert out[0]["duid"] is None


# ---------------------------------------------------------------------------
# queries.reconcile_request_for_mac()
# ---------------------------------------------------------------------------

def test_reconcile_request_for_mac_returns_latest_row(monkeypatch):
    """Returns the most recent reconcile_requests row as a dict."""
    import datetime
    from flax_control import queries

    ts = datetime.datetime(2026, 6, 17, 10, 0, 0)
    completed_ts = datetime.datetime(2026, 6, 17, 10, 5, 0)
    cursor = MagicMock()
    cursor.fetchone.return_value = (
        "operator bmc-reset", "done", 1, "Redfish reset OK", ts, completed_ts
    )
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    result = queries.reconcile_request_for_mac("aa:bb:cc:00:00:01")
    assert result is not None
    assert result["reason"] == "operator bmc-reset"
    assert result["status"] == "done"
    assert result["attempts"] == 1
    assert result["outcome"] == "Redfish reset OK"
    assert result["ts"] == ts.isoformat()
    assert result["completed_at"] == completed_ts.isoformat()


def test_reconcile_request_for_mac_returns_none_when_no_row(monkeypatch):
    """Returns None when there is no reconcile_requests row for the mac."""
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    result = queries.reconcile_request_for_mac("ff:ff:ff:ff:ff:ff")
    assert result is None


def test_reconcile_request_for_mac_sql_queries_by_mac_newest_first(monkeypatch):
    """SQL must filter by mac (case-insensitive) and order by ts DESC LIMIT 1."""
    from flax_control import queries

    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    queries.reconcile_request_for_mac("AA:BB:CC:00:11:22")

    sql, params = cursor.execute.call_args[0]
    assert "reconcile_requests" in sql
    assert "ORDER BY ts DESC" in sql
    assert "LIMIT 1" in sql
    # mac filter present (case-insensitive)
    assert "lower(mac)" in sql.lower() or "mac" in sql
    # param contains the mac
    assert any("aa:bb:cc:00:11:22" == str(v).lower() for v in (params if isinstance(params, (list, tuple)) else params.values()))


def test_reconcile_request_for_mac_handles_none_completed_at(monkeypatch):
    """completed_at=None (open request) is passed through as None in the dict."""
    from flax_control import queries
    import datetime

    ts = datetime.datetime(2026, 6, 17, 10, 0, 0)
    cursor = MagicMock()
    cursor.fetchone.return_value = (
        "operator flap", "pending", 0, None, ts, None
    )
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(queries, "get_pool", lambda: pool)

    result = queries.reconcile_request_for_mac("aa:bb:cc:00:00:01")
    assert result["status"] == "pending"
    assert result["completed_at"] is None
    assert result["ts"] == ts.isoformat()
