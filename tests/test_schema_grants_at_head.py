"""Service-role grants on the schema at head, read from the real per-run database
(conftest builds it from schema/ with alembic every run). One test per grant
boundary a migration set up, checked as the privilege postgres actually reports
rather than as text in the migration file. Replaces the per-migration SQL-text
tests for 025-034 (dropped 2026-11-12)."""
import os

import pytest

psycopg = pytest.importorskip("psycopg")

READERS = ("flax_control", "flax_reconcile", "flax_observe", "flax_switch_sense",
           "flax_discover", "flax_post")


@pytest.fixture(scope="module")
def cur():
    dsn = os.environ.get("FLAX_TEST_ADMIN_DSN")
    if not dsn:
        pytest.skip("no per-run test database (see conftest.py)")
    with psycopg.connect(dsn, autocommit=True) as c:
        yield c.cursor()


def _rel(cur, name):
    cur.execute("SELECT n.nspname || '.' || c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = %s AND n.nspname NOT IN ('pg_catalog', 'information_schema')",
                (name,))
    rows = cur.fetchall()
    assert len(rows) == 1, (name, rows)
    return rows[0][0]


def _privs(cur, role, name):
    rel = _rel(cur, name)
    out = set()
    for p in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        cur.execute("SELECT has_table_privilege(%s, %s, %s)", (role, rel, p))
        if cur.fetchone()[0]:
            out.add(p)
    return out


def _seq_usage(cur, role, name):
    cur.execute("SELECT has_sequence_privilege(%s, %s, 'USAGE')", (role, _rel(cur, name)))
    return cur.fetchone()[0]


RW = {"SELECT", "INSERT", "UPDATE", "DELETE"}


def test_post_owns_its_state_tables(cur):                       # 025, 032
    for t in ("post_state", "post_node", "post_settings", "post_artifact"):
        assert _privs(cur, "flax_post", t) == RW, t


def test_post_reads_switch_facts_and_classify_reads_post_settings(cur):   # 026, 027
    assert "SELECT" in _privs(cur, "flax_post", "switch_facts")
    assert _privs(cur, "flax_classify", "post_settings") == {"SELECT"}


def test_work_records_are_append_only_for_post(cur):            # 031
    for t in ("dut", "work_records"):
        assert _privs(cur, "flax_post", t) == {"SELECT", "INSERT"}, t
    for s in ("dut_dut_id_seq", "work_records_id_seq"):
        assert _seq_usage(cur, "flax_post", s), s


def test_only_post_may_run_the_retention_sweep(cur):            # 033
    cur.execute("SELECT has_function_privilege('flax_post', "
                "'work_records_retain(int, boolean, int)', 'EXECUTE'), "
                "has_function_privilege('flax_control', "
                "'work_records_retain(int, boolean, int)', 'EXECUTE')")
    assert cur.fetchone() == (True, False)


def test_post_sees_observe_identity_not_observe_state(cur):     # 034
    assert _privs(cur, "flax_post", "observe_identity") == {"SELECT"}
    assert _privs(cur, "flax_post", "observe_state") == set()


def test_classify_owns_the_role_registry_and_desired_tables(cur):   # 029, 030
    for t in ("roles", "role_universe", "desired_reservations", "mac_ownership_events",
              "materializer_plan"):
        assert _privs(cur, "flax_classify", t) == RW, t
        for svc in READERS:
            assert "SELECT" in _privs(cur, svc, t), (t, svc)
    for s in ("mac_ownership_events_id_seq", "materializer_plan_id_seq"):
        assert _seq_usage(cur, "flax_classify", s), s
