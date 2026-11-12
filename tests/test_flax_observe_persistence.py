"""Tests for flax_observe.persistence."""
from unittest.mock import MagicMock

import pytest


def test_upsert_observe_state_increments_generation(monkeypatch):
    from flax_observe import persistence

    cursor = MagicMock()
    cursor.fetchone.return_value = (17,)
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(persistence, "get_pool", lambda: pool)

    gen = persistence.upsert_observe_state(
        switch="rabbit-lorax", port="Et6/1",
        vars={"linkstate": {"value": "link", "since": "..."}},
        last_polled="2026-05-24T08:00:00Z",
    )
    assert gen == 17
    sql = cursor.execute.call_args[0][0]
    assert "INSERT INTO observe_state" in sql
    assert "ON CONFLICT (switch, port) DO UPDATE" in sql
    # Whitespace-tolerant: column alignment may pad before the `=`
    import re as _re
    assert _re.search(r"generation\s+=\s+observe_state\.generation \+ 1", sql)
    assert "RETURNING generation" in sql
    assert "resolved" in sql   # Plan 3.5 follow-up: scalar resolved fields persisted


def test_emit_audit_event_writes_row(monkeypatch):
    from flax_observe import persistence

    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(persistence, "get_pool", lambda: pool)

    persistence.emit_audit_event(
        kind="transition",
        switch="rabbit-lorax", port="Et6/1", mac=None,
        payload={"var": "linkstate", "from": "unknown", "to": "link"},
    )
    sql = cursor.execute.call_args[0][0]
    assert "INSERT INTO audit.events" in sql
    assert "service" in sql


def test_resolved_round_trips_product_name():
    """resolved is JSONB; the whole dict round-trips, so a new scalar like
    product_name needs no persistence.py change. SKIP if no DB is reachable.
    """
    from flax_observe import db
    from flax_observe.persistence import upsert_observe_state

    try:
        pool = db.get_pool()
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")

    upsert_observe_state(
        switch="rabbit-lorax", port="et6b1",
        vars={"linkstate": {"value": "link"}},
        last_polled="2026-06-15T00:00:00Z",
        resolved={"bmc_mac": "0c:42:a1:00:00:01",
                  "product_name": "Leopard-DDR4"},
    )
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT resolved->>'product_name' FROM observe_state "
            "WHERE switch=%s AND port=%s",
            ("rabbit-lorax", "et6b1")).fetchone()
    assert row[0] == "Leopard-DDR4"


def test_read_prior_observe_state_keys_by_switch_port(monkeypatch):
    from flax_observe import persistence

    rows = [
        ("rabbit-lorax", "et6b1",
         {"linkstate": {"value": "link", "since": "2026-06-20T00:00:00Z"}},
         {"nic_mac": "52:54:00:11:22:33", "link_session_since": "2026-06-20T00:00:00Z"}),
        ("rabbit-lorax", "et7b1", {"linkstate": {"value": "nolink", "since": None}}, {}),
    ]
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

    out = persistence.read_prior_observe_state(pool)

    sql = cursor.execute.call_args[0][0]
    assert "SELECT switch, port, vars, resolved FROM observe_state" in sql
    assert set(out) == {("rabbit-lorax", "et6b1"), ("rabbit-lorax", "et7b1")}
    assert out[("rabbit-lorax", "et6b1")]["vars"]["linkstate"]["value"] == "link"
    assert out[("rabbit-lorax", "et6b1")]["resolved"]["nic_mac"] == "52:54:00:11:22:33"
    # NULL/empty resolved coalesces to {}
    assert out[("rabbit-lorax", "et7b1")]["resolved"] == {}


def test_resolved_includes_bmc_kind(monkeypatch):
    """PortWorker persists bmc_kind (from bmc_kind_cached) into resolved."""
    from flax_observe import port_worker as pw

    captured = {}

    def fake_upsert(*, switch, port, vars, last_polled, resolved):
        captured["resolved"] = resolved
        return 1

    monkeypatch.setattr(pw, "upsert_observe_state", fake_upsert)
    monkeypatch.setattr(pw, "emit_audit_event", lambda **kw: None)

    # Built through the real constructor (as __main__ does) so the worker
    # carries every attribute _persist reads (env, for the optional
    # vendor_export side channel); a bare __new__ skipped __init__ and broke
    # when _persist grew that read (d43a05a).
    import types
    env = types.SimpleNamespace()  # make_env-like: no vendor_export wired
    cache = types.SimpleNamespace(get_port=lambda *a, **k: {})
    w = pw.PortWorker("rabbit-x", "et6b1", "20", cache, env, cycle_secs=10.0)
    w._boot_checked = True
    w.port_state.update({
        "bmc_kind_cached": {
            "kind": "openbmc",
            "creds_used": ("root", "test-obmc-pw"),
            "product_name": "Tioga Pass",
            "for_mac": "aa:bb:cc:dd:ee:ff",
        },
        "product_name": "Tioga Pass",
        "link_session_since": "2026-06-22T00:00:00Z",
    })
    w._persist()

    assert captured["resolved"]["bmc_kind"] == "openbmc"  # string, not the dict
    assert captured["resolved"]["product_name"] == "Tioga Pass"
    assert captured["resolved"]["link_session_since"] == "2026-06-22T00:00:00Z"
    # The probe dict (and especially creds_used) must NOT leak into resolved:
    assert captured["resolved"]["bmc_kind"] == "openbmc"
    assert "test-obmc-pw" not in str(captured["resolved"])


def _one_row_pool(row):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, cursor


def test_read_observe_state_row_returns_one_row_or_none():
    from flax_observe import persistence
    pool, cur = _one_row_pool(({"linkstate": {"value": "link", "since": None}},
                               {"bmc_mac": "b8:59:9f:ab:af:0e"}))
    out = persistence.read_observe_state_row(pool, "rabbit-edam", "et7b1")
    assert out == {"vars": {"linkstate": {"value": "link", "since": None}},
                   "resolved": {"bmc_mac": "b8:59:9f:ab:af:0e"}}
    sql, params = cur.execute.call_args[0]
    assert "FROM observe_state WHERE switch" in sql and params == ("rabbit-edam", "et7b1")
    pool, _ = _one_row_pool(None)
    assert persistence.read_observe_state_row(pool, "rabbit-edam", "et7b1") is None
    pool, _ = _one_row_pool(({"linkstate": {}}, None))
    assert persistence.read_observe_state_row(pool, "s", "p")["resolved"] == {}
