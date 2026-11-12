"""Migration 035: kea.flax_classify_release_leases compares lease6.address as inet.

023 wrote `DELETE FROM kea.lease6 WHERE address = ANY(p_v6_addrs)` with
p_v6_addrs text[]. Kea schema 22 made lease6.address inet, so every call that
carried a v6 address raised `operator does not exist: inet = text` -- and,
being one statement, rolled back the lease4 DELETE that had already run. The
classify sweep (post AND triage) therefore removed reservations but never their
leases; a slot's dead lease then made Kea refuse the incoming BMC its reserved
IP (ALLOC_ENGINE_V4_DISCOVER_ADDRESS_CONFLICT) until the lease expired.
Measured at eindhoven 2026-09-21: 156 post + 701 triage failures in one day.

Static tests load the migration from the cwd's code tree, falling back to the
imported package's tree (see test_flax_post_migration_034.py for why). DB tests run the INSTALLED function,
so they need `alembic upgrade head` on the LOCAL test cluster, and they refuse
any database without `_retention_scratch_marker` -- never a bang (memory
feedback-no-db-access-from-the-vm). Fixture rows use a locally administered
MAC, TEST-NET-2 and a documentation v6 prefix, and state=1 so the
lease_events triggers (which fire only for state 0) write nothing.
"""
import importlib.util
import os
import pathlib

import pytest

import flax_post

psycopg = pytest.importorskip("psycopg")

FN = "kea.flax_classify_release_leases(bytea[],text[])"
MAC = bytes.fromhex("02fe35000001")
V4 = 3325256961          # 198.51.100.1 (TEST-NET-2)
V6 = "2001:db8:35::1"    # documentation prefix


def _load():
    # cwd first: run-tests.sh runs from the code repo, and a worktree is only
    # reachable that way -- this tree's flax_post symlink always points at main.
    rel = "schema/versions/035_flax_classify_release_leases_inet.py"
    p = pathlib.Path.cwd() / rel
    if not p.exists():
        p = pathlib.Path(flax_post.__file__).resolve().parent.parent / rel
    spec = importlib.util.spec_from_file_location("m035", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sql(m, monkeypatch, fn="upgrade"):
    seen = []
    monkeypatch.setattr(m.op, "execute", lambda sql: seen.append(sql))
    getattr(m, fn)()
    return "\n".join(seen).lower()


def test_revision_chain():
    m = _load()
    assert m.revision == "035000000001"
    assert m.down_revision == "034000000001"


def test_upgrade_casts_v6_addrs_to_inet_and_keeps_signature(monkeypatch):
    blob = _sql(_load(), monkeypatch)
    assert "create or replace function kea.flax_classify_release_leases(" in blob
    assert "p_hwaddrs bytea[], p_v6_addrs text[]" in blob, \
        "signature must not change: the grant and both callers depend on it"
    assert "address = any(p_v6_addrs::inet[])" in blob


def test_downgrade_restores_the_023_body(monkeypatch):
    blob = _sql(_load(), monkeypatch, fn="downgrade")
    assert "address = any(p_v6_addrs)" in blob
    assert "::inet[]" not in blob


def _dsn() -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    return " ".join([
        f"host={os.environ.get('PGHOST', '127.0.0.1')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={os.environ.get('PGUSER', 'postgres')}",
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
    ] + ([f"password={os.environ['PGPASSWORD']}"] if os.environ.get("PGPASSWORD") else []))


def _clean(c):
    c.execute("DELETE FROM kea.lease4 WHERE hwaddr = %s", (MAC,))
    c.execute("DELETE FROM kea.lease6 WHERE address = %s::inet", (V6,))


@pytest.fixture()
def conn():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    marker = c.execute("SELECT to_regclass('public._retention_scratch_marker')").fetchone()[0]
    if marker is None:
        c.close()
        pytest.skip("not the local scratch cluster (no _retention_scratch_marker)")
    _clean(c)
    try:
        yield c
    finally:
        _clean(c)
        c.close()


def _seed(c):
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, subnet_id, state) "
              "VALUES (%s, %s, 25, 1)", (V4, MAC))
    c.execute("INSERT INTO kea.lease6 (address, hwaddr, subnet_id, state) "
              "VALUES (%s::inet, %s, 25, 1)", (V6, MAC))


def _counts(c):
    v4 = c.execute("SELECT count(*) FROM kea.lease4 WHERE hwaddr = %s", (MAC,)).fetchone()[0]
    v6 = c.execute("SELECT count(*) FROM kea.lease6 WHERE address = %s::inet", (V6,)).fetchone()[0]
    return v4, v6


def test_release_with_v6_addrs_deletes_both_leases(conn):
    """The eindhoven shape: a swept host with a v6 reservation. On 023 this
    raised inet = text and the lease4 row survived with it."""
    _seed(conn)
    conn.execute("SELECT kea.flax_classify_release_leases(%s::bytea[], %s::text[])",
                 ([MAC], [V6]))
    assert _counts(conn) == (0, 0)


def test_release_v4_only_leaves_v6_alone(conn):
    _seed(conn)
    conn.execute("SELECT kea.flax_classify_release_leases(%s::bytea[], %s::text[])",
                 ([MAC], []))
    assert _counts(conn) == (0, 1)


def test_fn_stays_definer_with_classify_grant(conn):
    row = conn.execute(
        "SELECT p.prosecdef, has_function_privilege('flax_classify', %s, 'EXECUTE') "
        "FROM pg_proc p WHERE p.oid = %s::regprocedure", (FN, FN)).fetchone()
    assert row == (True, True)
