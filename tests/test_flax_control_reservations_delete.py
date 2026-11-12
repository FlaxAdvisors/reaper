"""Tests for flax_control.reservations_delete (hard DELETE of a kea.hosts row).

A reservation whose device is gone won't be re-created by flax-classify (it only
upserts current occupants; legacy-import is a one-shot CLI), so a plain DELETE is
durable. The v6 row cascades via the kea.ipv6_reservations FK (ON DELETE CASCADE).
"""
import pytest
from unittest.mock import MagicMock


def _mock_pool(rowcount):
    """Pool whose single execute() (the DELETE) returns a cursor with rowcount."""
    cur = MagicMock()
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.execute.return_value = cur
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn


def test_delete_reservation_issues_scoped_delete_and_returns_true(monkeypatch):
    from flax_control import reservations_delete

    pool, conn = _mock_pool(rowcount=1)
    monkeypatch.setattr(reservations_delete, "get_pool", lambda: pool)

    deleted = reservations_delete.delete_reservation("1c34da7fb3a4")

    assert deleted is True
    assert conn.execute.call_count == 1
    sql, params = conn.execute.call_args_list[0][0]
    assert "DELETE FROM kea.hosts" in sql
    # MAC-keyed only (type 0); never touch DUID-keyed rows
    assert "dhcp_identifier = decode(%s, 'hex')" in sql
    assert "dhcp_identifier_type = 0" in sql
    assert params == ("1c34da7fb3a4",)


def test_delete_reservation_returns_false_when_already_gone(monkeypatch):
    """rowcount 0 -> nothing matched (idempotent); not an error."""
    from flax_control import reservations_delete

    pool, conn = _mock_pool(rowcount=0)
    monkeypatch.setattr(reservations_delete, "get_pool", lambda: pool)

    assert reservations_delete.delete_reservation("aaaaaaaaaaaa") is False


def test_delete_reservation_bad_hex_raises_notfound_without_db(monkeypatch):
    """Non-hex / wrong-length input raises NotFound BEFORE any Postgres call
    (avoids a psycopg DataError bubbling up as a 500)."""
    from flax_control import reservations_delete

    pool = MagicMock()
    monkeypatch.setattr(reservations_delete, "get_pool", lambda: pool)

    with pytest.raises(reservations_delete.NotFound):
        reservations_delete.delete_reservation("1c34da7fb3aZ")  # non-hex
    with pytest.raises(reservations_delete.NotFound):
        reservations_delete.delete_reservation("deadbeef")      # 8 chars
    pool.connection.assert_not_called()
