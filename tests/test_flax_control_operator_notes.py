"""Tests for flax_control.operator_notes (read-merge-write on user_context)."""
import pytest
from unittest.mock import MagicMock


def _mock_pool_with_select(fetchone_return):
    """Build a pool where the FIRST execute returns fetchone_return
    (the SELECT) and subsequent executes succeed silently (the UPDATE)."""
    select_cursor = MagicMock()
    select_cursor.fetchone.return_value = fetchone_return
    select_cursor.__enter__.return_value = select_cursor
    select_cursor.__exit__.return_value = False
    update_cursor = MagicMock()
    update_cursor.__enter__.return_value = update_cursor
    update_cursor.__exit__.return_value = False
    conn = MagicMock()
    # First call returns select_cursor, second returns update_cursor
    conn.execute.side_effect = [select_cursor, update_cursor]
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn, select_cursor, update_cursor


def test_update_operator_note_merges_into_existing_user_context(monkeypatch):
    """SELECT existing user_context (TEXT JSON), set .operator_note, UPDATE.
    Existing keys (classify) must be preserved."""
    import json
    from flax_control import operator_notes

    pool, conn, sel, upd = _mock_pool_with_select(
        # Real Kea schema: user_context is TEXT with JSON content
        fetchone_return=(json.dumps({"classify": {"switch": "rabbit-lorax",
                                                   "port": "et6b1",
                                                   "kind": "bmc",
                                                   "vid": 17}}),))
    monkeypatch.setattr(operator_notes, "get_pool", lambda: pool)

    operator_notes.update_operator_note("1c34da7fb3a4", "flaky bmc")

    # 2 calls: SELECT then UPDATE
    assert conn.execute.call_count == 2
    select_sql = conn.execute.call_args_list[0][0][0]
    update_sql, update_params = conn.execute.call_args_list[1][0]
    assert "SELECT user_context FROM kea.hosts" in select_sql
    # Real Kea schema: WHERE on (dhcp_identifier, dhcp_identifier_type=0)
    assert "dhcp_identifier" in select_sql
    assert "dhcp_identifier_type = 0" in select_sql
    assert "UPDATE kea.hosts" in update_sql
    assert "SET user_context" in update_sql
    # The UPDATE param is a JSON string (user_context is TEXT in Kea)
    merged = json.loads(update_params[0])
    assert merged["classify"]["switch"] == "rabbit-lorax"
    assert merged["operator_note"] == "flaky bmc"


def test_update_operator_note_empty_string_clears_the_key(monkeypatch):
    """note='' removes the operator_note key entirely (rather than
    storing an empty string), so the view's COALESCE works cleanly."""
    import json
    from flax_control import operator_notes

    pool, conn, sel, upd = _mock_pool_with_select(
        fetchone_return=(json.dumps({"classify": {"switch": "x"},
                                      "operator_note": "old"}),))
    monkeypatch.setattr(operator_notes, "get_pool", lambda: pool)

    operator_notes.update_operator_note("1c34da7fb3a4", "")
    update_params = conn.execute.call_args_list[1][0][1]
    merged = json.loads(update_params[0])
    assert "operator_note" not in merged
    assert merged["classify"]["switch"] == "x"  # unrelated key preserved


def test_update_operator_note_raises_for_missing_reservation(monkeypatch):
    """SELECT returns None -> raise NotFound (the route turns this
    into a 404)."""
    from flax_control import operator_notes

    pool, conn, sel, upd = _mock_pool_with_select(fetchone_return=None)
    monkeypatch.setattr(operator_notes, "get_pool", lambda: pool)

    with pytest.raises(operator_notes.NotFound):
        operator_notes.update_operator_note("deadbeefdead", "anything")
    # No UPDATE issued
    assert conn.execute.call_count == 1


def test_update_operator_note_handles_null_user_context(monkeypatch):
    """SELECT returns (None,) when user_context is NULL -- merging
    works from a clean slate."""
    import json
    from flax_control import operator_notes

    pool, conn, sel, upd = _mock_pool_with_select(fetchone_return=(None,))
    monkeypatch.setattr(operator_notes, "get_pool", lambda: pool)

    operator_notes.update_operator_note("aaaaaaaaaaaa", "n")
    update_params = conn.execute.call_args_list[1][0][1]
    merged = json.loads(update_params[0])
    assert merged == {"operator_note": "n"}


def test_update_operator_note_raises_notfound_for_bad_hex(monkeypatch):
    """Non-hex characters in mac_hex must raise NotFound BEFORE any
    Postgres call (avoids leaking a DataError 500 from psycopg)."""
    from flax_control import operator_notes

    pool = MagicMock()
    monkeypatch.setattr(operator_notes, "get_pool", lambda: pool)

    with pytest.raises(operator_notes.NotFound):
        operator_notes.update_operator_note("1c34da7fb3aZ", "anything")
    # Pool was never touched -- no SELECT, no UPDATE
    pool.connection.assert_not_called()


def test_update_operator_note_raises_notfound_for_wrong_length_hex(monkeypatch):
    from flax_control import operator_notes
    pool = MagicMock()
    monkeypatch.setattr(operator_notes, "get_pool", lambda: pool)

    with pytest.raises(operator_notes.NotFound):
        operator_notes.update_operator_note("deadbeef", "anything")  # 8 chars
    pool.connection.assert_not_called()
