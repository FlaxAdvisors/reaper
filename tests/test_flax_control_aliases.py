"""Tests for flax_control.aliases (DNS hostname aliases on user_context.aliases)."""
import json
import pytest
from unittest.mock import MagicMock


def _mock_pool_with_select(fetchone_return):
    select_cursor = MagicMock()
    select_cursor.fetchone.return_value = fetchone_return
    update_cursor = MagicMock()
    conn = MagicMock()
    conn.execute.side_effect = [select_cursor, update_cursor]
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn


# --- parse_aliases (pure) ---

def test_parse_splits_whitespace_lowercases_dedups():
    from flax_control import aliases
    assert aliases.parse_aliases("Foo  bar   foo BAZ") == ["foo", "bar", "baz"]


def test_parse_empty_or_whitespace_is_empty_list():
    from flax_control import aliases
    assert aliases.parse_aliases("") == []
    assert aliases.parse_aliases("   \t ") == []


def test_parse_allows_fqdn_with_dots_and_hyphens():
    from flax_control import aliases
    assert aliases.parse_aliases("db-1.brt.lab gw-2") == ["db-1.brt.lab", "gw-2"]


@pytest.mark.parametrize("bad", ["bad_name", "-lead", "trail-", ".dot", "a..b", "white space!"])
def test_parse_rejects_non_dns_tokens(bad):
    from flax_control import aliases
    with pytest.raises(aliases.InvalidAlias):
        aliases.parse_aliases(bad)


# --- update_aliases (read-merge-write) ---

def test_update_sets_aliases_preserving_other_keys(monkeypatch):
    from flax_control import aliases
    pool, conn = _mock_pool_with_select(
        fetchone_return=(json.dumps({"classify": {"switch": "x"},
                                     "operator_note": "keep me"}),))
    monkeypatch.setattr(aliases, "get_pool", lambda: pool)

    out = aliases.update_aliases("1c34da7fb3a4", "alias1 alias2")

    assert out == ["alias1", "alias2"]
    update_sql, params = conn.execute.call_args_list[1][0]
    assert "UPDATE kea.hosts" in update_sql and "SET user_context" in update_sql
    merged = json.loads(params[0])
    assert merged["aliases"] == ["alias1", "alias2"]
    assert merged["classify"]["switch"] == "x"        # preserved
    assert merged["operator_note"] == "keep me"       # preserved


def test_update_empty_clears_aliases_key(monkeypatch):
    from flax_control import aliases
    pool, conn = _mock_pool_with_select(
        fetchone_return=(json.dumps({"classify": {"a": 1},
                                     "aliases": ["old"]}),))
    monkeypatch.setattr(aliases, "get_pool", lambda: pool)

    aliases.update_aliases("1c34da7fb3a4", "")
    merged = json.loads(conn.execute.call_args_list[1][0][1][0])
    assert "aliases" not in merged
    assert merged["classify"] == {"a": 1}


def test_update_missing_row_raises_notfound(monkeypatch):
    from flax_control import aliases
    pool, conn = _mock_pool_with_select(fetchone_return=None)
    monkeypatch.setattr(aliases, "get_pool", lambda: pool)
    with pytest.raises(aliases.NotFound):
        aliases.update_aliases("deadbeefdead", "a")
    assert conn.execute.call_count == 1  # no UPDATE


def test_update_bad_hex_raises_notfound_without_db(monkeypatch):
    from flax_control import aliases
    pool = MagicMock()
    monkeypatch.setattr(aliases, "get_pool", lambda: pool)
    with pytest.raises(aliases.NotFound):
        aliases.update_aliases("nothex!!!!!!", "a")
    pool.connection.assert_not_called()


def test_update_invalid_alias_raises_before_db(monkeypatch):
    from flax_control import aliases
    pool = MagicMock()
    monkeypatch.setattr(aliases, "get_pool", lambda: pool)
    with pytest.raises(aliases.InvalidAlias):
        aliases.update_aliases("1c34da7fb3a4", "good bad_one")
    pool.connection.assert_not_called()
