"""Tests for flax_classify.desired_reservations — desired-row writer +
mac-ownership handoff ledger (shadow materialization phase 2, Task 2).

DB-backed tests: skip convention COPIED from
tests/test_flax_classify_desired_port.py (credit: same skip pattern, same
_dsn()/pool helpers) since no local Postgres is available in this
environment. They SKIP here and run for real on gouda at deploy
(FLAX_TEST_DSN).
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")


# ── fixtures (copied/adapted from test_flax_classify_desired_port.py) ──────

TEST_MAC = "aa:bb:cc:00:11:22"


def _dsn() -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    return " ".join([
        "host=" + os.environ.get("PGHOST", "127.0.0.1"),
        "port=" + os.environ.get("PGPORT", "5432"),
        "user=" + os.environ.get("PGUSER", "postgres"),
        "password=" + os.environ.get("PGPASSWORD", ""),
        "dbname=" + os.environ.get("PGDATABASE", "flax"),
    ])


def _cleanup(conn) -> None:
    conn.execute("DELETE FROM mac_ownership_events WHERE mac = %s", (TEST_MAC,))
    conn.execute("DELETE FROM desired_reservations WHERE mac = %s", (TEST_MAC,))


@pytest.fixture()
def dr_conn():
    """Scoped connection + cleanup; SKIP if no DB."""
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:
        pytest.skip("no DB available: " + str(exc))
    try:
        _cleanup(c)
        yield c
    finally:
        try:
            _cleanup(c)
        except Exception:
            pass
        c.close()


def _build_pool(dsn: str):
    from flax_classify.db import build_pool
    return build_pool(dsn)


def _upsert_kwargs(**overrides):
    kwargs = dict(
        owner_role="triage", mac=TEST_MAC, kind="bmc",
        hostname="et6b1-bmc", ipv4="10.0.0.5", ipv6=None,
        vid=16, switch="rabbit-test-dr", port="et6b1",
    )
    kwargs.update(overrides)
    return kwargs


# ── DB-backed tests ─────────────────────────────────────────────────────────

def test_upsert_then_read_roundtrip(dr_conn):
    from flax_classify.desired_reservations import upsert_desired, read_desired

    pool = _build_pool(_dsn())
    try:
        prev = upsert_desired(pool, **_upsert_kwargs())
        assert prev is None, "first-ever upsert has no previous owner"

        rows = [r for r in read_desired(pool) if r["mac"] == TEST_MAC]
        assert len(rows) == 1
        row = rows[0]
        assert row["owner_role"] == "triage"
        assert row["kind"] == "bmc"
        assert row["hostname"] == "et6b1-bmc"
        assert row["ipv4"] == "10.0.0.5"
        assert row["vid"] == 16
        assert row["switch"] == "rabbit-test-dr"
        assert row["port"] == "et6b1"
        assert row["generation"] == 1
    finally:
        pool.close()


def test_second_identical_upsert_bumps_generation(dr_conn):
    from flax_classify.desired_reservations import upsert_desired

    pool = _build_pool(_dsn())
    try:
        upsert_desired(pool, **_upsert_kwargs())
        upsert_desired(pool, **_upsert_kwargs())
        row = dr_conn.execute(
            "SELECT generation FROM desired_reservations WHERE mac = %s",
            (TEST_MAC,),
        ).fetchone()
        assert row is not None
        assert row[0] == 2, "generation should bump to 2 on re-upsert"
    finally:
        pool.close()


def test_owner_flip_writes_one_ledger_row_and_returns_old_owner(dr_conn):
    from flax_classify.desired_reservations import upsert_desired

    pool = _build_pool(_dsn())
    try:
        prev1 = upsert_desired(pool, **_upsert_kwargs(owner_role="triage"))
        assert prev1 is None

        prev2 = upsert_desired(pool, **_upsert_kwargs(
            owner_role="post", switch="rabbit-test-dr", port="et6b1"))
        assert prev2 == "triage", "should return the prior owner on a handoff"

        rows = dr_conn.execute(
            "SELECT from_role, to_role, switch, port FROM mac_ownership_events "
            "WHERE mac = %s",
            (TEST_MAC,),
        ).fetchall()
        assert len(rows) == 1, "exactly one ledger row for the handoff"
        from_role, to_role, switch, port = rows[0]
        assert from_role == "triage"
        assert to_role == "post"
        assert switch == "rabbit-test-dr"
        assert port == "et6b1"

        # same-owner re-upsert must NOT write another ledger row
        upsert_desired(pool, **_upsert_kwargs(owner_role="post"))
        rows2 = dr_conn.execute(
            "SELECT id FROM mac_ownership_events WHERE mac = %s", (TEST_MAC,),
        ).fetchall()
        assert len(rows2) == 1, "no new ledger row on a same-owner re-upsert"
    finally:
        pool.close()


def test_sweep_desired_not_in_scoped_to_owner(dr_conn):
    """`sweep_desired_not_in(..., keep_macs=[])` issues a bare
    `DELETE ... WHERE owner_role = %s` with NO mac filter at all -- calling
    it with the REAL "triage" owner_role would wipe every live triage
    desired_reservations row on a post-roll re-run of this test against
    gouda (this is a DB test, not a hermetic one: no local Postgres runs it,
    it runs for real at deploy). "triage-test" is never a registered owner
    role, so this sweep can only ever touch the one row this test itself
    seeded, while the assertion that a DIFFERENT owner's row survives is
    kept (scoped this time to the real "post" owner, whose row was never at
    risk either way since the sweep's WHERE clause is owner_role-scoped).
    """
    from flax_classify.desired_reservations import upsert_desired, sweep_desired_not_in

    SWEEP_OWNER = "triage-test"
    other_mac = "aa:bb:cc:00:11:23"
    pool = _build_pool(_dsn())
    try:
        upsert_desired(pool, **_upsert_kwargs(owner_role=SWEEP_OWNER, mac=TEST_MAC))
        upsert_desired(pool, **_upsert_kwargs(owner_role="post", mac=other_mac))
        try:
            deleted = sweep_desired_not_in(pool, owner_role=SWEEP_OWNER, keep_macs=[])
            assert deleted == 1

            remaining = dr_conn.execute(
                "SELECT mac, owner_role FROM desired_reservations "
                "WHERE mac IN (%s, %s)",
                (TEST_MAC, other_mac),
            ).fetchall()
            remaining_macs = {r[0] for r in remaining}
            assert TEST_MAC not in remaining_macs, "throwaway owner's absent mac should be swept"
            assert other_mac in remaining_macs, "the other owner's row must be untouched"
        finally:
            dr_conn.execute(
                "DELETE FROM desired_reservations WHERE mac = %s", (other_mac,))
            dr_conn.execute(
                "DELETE FROM mac_ownership_events WHERE mac = %s", (other_mac,))
    finally:
        pool.close()


def test_delete_desired_slot_keeps_keep_mac(dr_conn):
    from flax_classify.desired_reservations import upsert_desired, delete_desired_slot

    other_mac = "aa:bb:cc:00:11:24"
    pool = _build_pool(_dsn())
    try:
        upsert_desired(pool, **_upsert_kwargs(
            owner_role="post", mac=TEST_MAC, kind="bmc",
            switch="rabbit-test-dr", port="et6b1"))
        upsert_desired(pool, **_upsert_kwargs(
            owner_role="post", mac=other_mac, kind="bmc",
            switch="rabbit-test-dr", port="et6b1"))
        try:
            deleted = delete_desired_slot(
                pool, owner_role="post", switch="rabbit-test-dr", port="et6b1",
                kind="bmc", keep_mac=TEST_MAC)
            assert deleted == 1

            remaining = dr_conn.execute(
                "SELECT mac FROM desired_reservations "
                "WHERE switch = %s AND port = %s AND kind = %s",
                ("rabbit-test-dr", "et6b1", "bmc"),
            ).fetchall()
            remaining_macs = {r[0] for r in remaining}
            assert remaining_macs == {TEST_MAC}
        finally:
            dr_conn.execute(
                "DELETE FROM desired_reservations WHERE mac = %s", (other_mac,))
            dr_conn.execute(
                "DELETE FROM mac_ownership_events WHERE mac = %s", (other_mac,))
    finally:
        pool.close()


def test_delete_desired_removes_only_owners_macs(dr_conn):
    from flax_classify.desired_reservations import upsert_desired, delete_desired

    pool = _build_pool(_dsn())
    try:
        upsert_desired(pool, **_upsert_kwargs(owner_role="triage"))
        deleted_wrong_owner = delete_desired(pool, owner_role="post", macs=[TEST_MAC])
        assert deleted_wrong_owner == 0, "delete must be scoped to owner_role"

        deleted = delete_desired(pool, owner_role="triage", macs=[TEST_MAC])
        assert deleted == 1

        row = dr_conn.execute(
            "SELECT 1 FROM desired_reservations WHERE mac = %s", (TEST_MAC,),
        ).fetchone()
        assert row is None
    finally:
        pool.close()


def test_mac_is_normalized_on_upsert(dr_conn):
    from flax_classify.desired_reservations import upsert_desired

    pool = _build_pool(_dsn())
    try:
        upsert_desired(pool, **_upsert_kwargs(mac="AA:BB:CC:00:11:22"))
        row = dr_conn.execute(
            "SELECT mac FROM desired_reservations WHERE mac = %s", (TEST_MAC,),
        ).fetchone()
        assert row is not None, "mac should be stored lowercase-normalized"
    finally:
        pool.close()


# ── pure test (no DB) ───────────────────────────────────────────────────────

def test_norm_mac_lowercases():
    from flax_classify.desired_reservations import _norm_mac

    assert _norm_mac("AA:BB:CC:00:11:22") == "aa:bb:cc:00:11:22"
    assert _norm_mac("aa-bb-cc-00-11-22") == "aa:bb:cc:00:11:22"


# ── DB-free: delete_desired_slot reports WHICH macs it deleted ───────────────

def test_delete_desired_slot_reports_deleted_macs_via_returning():
    from unittest import mock
    from flax_classify.desired_reservations import delete_desired_slot

    cur = mock.MagicMock()
    cur.rowcount = 2
    cur.fetchall.return_value = [("98:03:9B:75:2A:B2",), ("aa:bb:cc:00:11:24",)]
    conn = mock.MagicMock()
    conn.execute.return_value = cur
    pool = mock.MagicMock()
    pool.connection.return_value.__enter__.return_value = conn

    got = set()
    n = delete_desired_slot(pool, owner_role="post", switch="rabbit-test-dr",
                            port="et28b3", kind="bmc",
                            keep_mac="98:03:9b:a8:27:6e", deleted_macs=got)
    assert n == 2                                  # rowcount semantics kept
    assert got == {"98:03:9b:75:2a:b2", "aa:bb:cc:00:11:24"}
    assert "RETURNING mac" in conn.execute.call_args.args[0]

    # without the out-param: same count, no crash (old call shape)
    assert delete_desired_slot(pool, owner_role="post", switch="rabbit-test-dr",
                               port="et28b3", kind="bmc",
                               keep_mac="98:03:9b:a8:27:6e") == 2
