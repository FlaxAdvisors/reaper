"""Registry capability lookup for flax-reconcile (Task 1, spec 2026-07-03
spine migration): replaces the hardcoded `source <> 'post'` literal in
db.read_reservations with a declared role capability
(capabilities.reconcile_switch).

All tests here are DB-free: a fake pool/connection (same shape as
tests/test_write_ack.py's _FakePool) stands in for psycopg, so these always
run regardless of local Postgres availability.
"""
import logging

from flax_reconcile import role_caps


# ---------------------------------------------------------------------------
# Fake pool/connection (mirrors tests/test_write_ack.py's _FakePool shape).
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows=None, exc=None):
        self._rows = rows if rows is not None else []
        self._exc = exc

    def execute(self, sql):
        if self._exc is not None:
            raise self._exc
        self.last_sql = sql
        return _FakeCursor(self._rows)


class _FakeCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows=None, exc=None):
        self.conn = _FakeConn(rows, exc)

    def connection(self):
        return _FakeCtx(self.conn)


# ---------------------------------------------------------------------------
# _eligible_from_rows: pure capability parsing.
# ---------------------------------------------------------------------------
def test_role_eligible_when_reconcile_switch_true():
    rows = [("triage", {"capabilities": {"reconcile_switch": True}})]
    assert role_caps._eligible_from_rows(rows) == frozenset({"triage", "legacy-import", ""})


def test_role_ineligible_when_reconcile_switch_false():
    rows = [("post", {"capabilities": {"reconcile_switch": False}})]
    result = role_caps._eligible_from_rows(rows)
    assert "post" not in result
    # Always-eligible legacy sources still present.
    assert result == frozenset({"legacy-import", ""})


def test_role_missing_capabilities_key_not_eligible():
    rows = [("mystery", {"policy": {}})]
    result = role_caps._eligible_from_rows(rows)
    assert "mystery" not in result


def test_role_missing_reconcile_switch_key_not_eligible():
    rows = [("mystery", {"capabilities": {"dhcp_reservations": True}})]
    result = role_caps._eligible_from_rows(rows)
    assert "mystery" not in result


def test_role_definition_none_not_eligible():
    rows = [("broken", None)]
    result = role_caps._eligible_from_rows(rows)
    assert "broken" not in result


def test_multiple_roles_mixed_eligibility():
    rows = [
        ("triage", {"capabilities": {"reconcile_switch": True}}),
        ("post", {"capabilities": {"reconcile_switch": False}}),
    ]
    result = role_caps._eligible_from_rows(rows)
    assert result == frozenset({"triage", "legacy-import", ""})


def test_always_eligible_present_even_with_no_rows():
    assert role_caps._eligible_from_rows([]) == frozenset({"legacy-import", ""})


# ---------------------------------------------------------------------------
# read_reconcile_eligible_sources: DB read + empty/error fallback.
# ---------------------------------------------------------------------------
def test_read_reconcile_eligible_sources_normal_case():
    pool = _FakePool(rows=[
        ("triage", {"capabilities": {"reconcile_switch": True}}),
        ("post", {"capabilities": {"reconcile_switch": False}}),
    ])
    result = role_caps.read_reconcile_eligible_sources(pool)
    assert result == frozenset({"triage", "legacy-import", ""})


def test_read_reconcile_eligible_sources_queries_roles_table():
    pool = _FakePool(rows=[])
    role_caps.read_reconcile_eligible_sources(pool)
    assert "SELECT role, definition FROM roles" in pool.conn.last_sql


def test_read_reconcile_eligible_sources_empty_table_returns_none_and_warns(caplog):
    pool = _FakePool(rows=[])
    with caplog.at_level(logging.WARNING, logger="flax-reconcile.role_caps"):
        result = role_caps.read_reconcile_eligible_sources(pool)
    assert result is None
    assert any("roles registry empty" in r.message for r in caplog.records)
    assert any("source<>'post'" in r.message for r in caplog.records)


def test_read_reconcile_eligible_sources_query_error_returns_none_and_logs(caplog):
    pool = _FakePool(exc=RuntimeError("connection reset"))
    with caplog.at_level(logging.WARNING, logger="flax-reconcile.role_caps"):
        result = role_caps.read_reconcile_eligible_sources(pool)
    assert result is None
    assert any("roles registry empty" in r.message for r in caplog.records)
    # log.exception path: a traceback/exc_info is attached to the record.
    assert any(r.exc_info is not None for r in caplog.records)


# ---------------------------------------------------------------------------
# Parity: the registry-driven frozenset filter must select exactly what the
# legacy `source <> 'post'` literal selects, for the fixture sources named in
# the task contract: 'triage', 'post', 'legacy-import', ''.
# ---------------------------------------------------------------------------
def _legacy_selects(source: str) -> bool:
    """Mirrors the SQL predicate `source <> 'post'` (post-COALESCE)."""
    return source != "post"


def _registry_selects(source: str, eligible_sources: frozenset) -> bool:
    """Mirrors the SQL predicate `source = ANY(eligible_sources)`."""
    return source in eligible_sources


def test_parity_registry_filter_matches_legacy_filter_live_registry_shape():
    # The LIVE registry shape: triage reconcile_switch=true, post=false
    # (roles/apply_lease_agent/templates/roles.d/{triage,post}.json.j2).
    rows = [
        ("triage", {"capabilities": {"reconcile_switch": True}}),
        ("post", {"capabilities": {"reconcile_switch": False}}),
    ]
    eligible = role_caps._eligible_from_rows(rows)

    fixture_sources = ["triage", "post", "legacy-import", ""]
    legacy_selected = {s for s in fixture_sources if _legacy_selects(s)}
    registry_selected = {s for s in fixture_sources
                        if _registry_selects(s, eligible)}

    assert legacy_selected == registry_selected == {"triage", "legacy-import", ""}
    # 'post' is excluded by both; 'legacy-import' and '' survive both.
    assert "post" not in registry_selected
    assert "post" not in legacy_selected


# --- reconcile_pool_bmc (2026-11-06) -----------------------------------------
def test_pool_bmc_capability_opt_in():
    assert role_caps._pool_bmc_from_rows([
        ("post", {"capabilities": {"reconcile_pool_bmc": True}}),
        ("triage", {"capabilities": {"reconcile_switch": True}})]) == frozenset({"post"})


def test_pool_bmc_capability_absent_is_empty():
    assert role_caps._pool_bmc_from_rows([("post", {"capabilities": {}}),
                                          ("x", None)]) == frozenset()


def test_read_pool_bmc_sources_unreadable_registry_is_empty():
    class _Boom:
        def connection(self):
            raise RuntimeError("db down")
    assert role_caps.read_pool_bmc_sources(_Boom()) == frozenset()
