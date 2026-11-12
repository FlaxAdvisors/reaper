"""Tests for flax_classify.db (read-side: observe_state + switch_facts)."""
from unittest.mock import MagicMock


def _mock_pool(fetchone_return=None, rowcount=0):
    """Helper: build a pool that yields a cursor with the given fetchone
    return value and rowcount. Mirrors test_flax_observe_persistence pattern.
    """
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.rowcount = rowcount
    conn = MagicMock()
    conn.execute.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn, cursor


def test_read_observe_rows_shapes_to_dicts():
    from flax_classify.db import read_observe_rows
    pool, conn, cursor = _mock_pool()
    cursor.fetchall.return_value = [
        ("rabbit-lorax", "et6b1",
         {"bmc_mac": "1c:34:da:7f:b3:a4", "nic_mac": "1c:34:da:7f:b3:a2"}),
        ("rabbit-lorax", "et6b2", None),  # NULL resolved -> {} in output
    ]
    rows = read_observe_rows(pool)
    assert rows == [
        {"switch": "rabbit-lorax", "port": "et6b1",
         "resolved": {"bmc_mac": "1c:34:da:7f:b3:a4",
                      "nic_mac": "1c:34:da:7f:b3:a2"}},
        {"switch": "rabbit-lorax", "port": "et6b2", "resolved": {}},
    ]


def test_read_switch_facts_groups_by_switch():
    from flax_classify.db import read_switch_facts
    pool, conn, cursor = _mock_pool()
    cursor.fetchall.return_value = [
        ("rabbit-lorax", {"Ethernet6/1": {"access_vid": 17}}),
    ]
    out = read_switch_facts(pool)
    assert out == {"rabbit-lorax":
                   {"ports": {"Ethernet6/1": {"access_vid": 17}}}}


# ---------------------------------------------------------------------------
# read_post_order tests
# ---------------------------------------------------------------------------

def _mock_pool_fetchone(fetchone_return):
    """Minimal pool mock for fetchone-based tests (read_post_order)."""
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    conn = MagicMock()
    conn.execute.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn


def test_read_post_order_returns_value():
    from flax_classify.db import read_post_order
    pool, conn = _mock_pool_fetchone(("RRORD-003",))
    assert read_post_order(pool) == "RRORD-003"
    sql = conn.execute.call_args[0][0]
    assert "post_settings" in sql and "order_no" in sql


def test_read_post_order_none_when_unset():
    from flax_classify.db import read_post_order
    pool, _ = _mock_pool_fetchone((None,))
    assert read_post_order(pool) is None
    pool2, _ = _mock_pool_fetchone(None)            # no row at all
    assert read_post_order(pool2) is None
