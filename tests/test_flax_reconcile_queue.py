"""Work-queue ops for reconcile_requests: enqueue/claim/complete/defer.

Tests require a real Postgres with migration 015 applied. No local Postgres
→ tests SKIP (expected).  Mirrors the skip convention from
test_flax_reconcile_db.py: importorskip psycopg, attempt connect, skip on
failure, scoped cleanup before and after each test.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from flax_reconcile import db, queue  # noqa: E402


def _dsn() -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    return " ".join([
        f"host={os.environ.get('PGHOST', '127.0.0.1')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={os.environ.get('PGUSER', 'postgres')}",
        f"password={os.environ.get('PGPASSWORD', '')}",
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
    ])


TEST_MACS = ("aa:bb:cc:00:00:01", "m1", "m2", "m3", "m4")


def _cleanup(conn) -> None:
    conn.execute(
        "DELETE FROM reconcile_requests WHERE mac = ANY(%s)", (list(TEST_MACS),))


@pytest.fixture()
def conn():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        _cleanup(c)
        yield c
    finally:
        try:
            _cleanup(c)
        except Exception:
            pass
        c.close()


@pytest.fixture()
def pool(conn):  # noqa: F811 - conn ensures DB is reachable before pool open
    p = db.build_pool(_dsn())
    try:
        yield p
    finally:
        p.close()


# ---------------------------------------------------------------------------
# enqueue dedup
# ---------------------------------------------------------------------------

def test_enqueue_dedups_open_requests(conn, pool):
    queue.enqueue(pool, mac="aa:bb:cc:00:00:01", requested_by="auto:mismatch",
                  reason="lease!=reservation", switch="s", port="p", kind="bmc")
    queue.enqueue(pool, mac="aa:bb:cc:00:00:01", requested_by="auto:sweep",
                  reason="lease!=reservation", switch="s", port="p", kind="bmc")
    n = conn.execute(
        "SELECT count(*) FROM reconcile_requests WHERE mac='aa:bb:cc:00:00:01' "
        "AND status='pending'").fetchone()[0]
    assert n == 1  # second enqueue absorbed by the open-mac unique index


# ---------------------------------------------------------------------------
# claim + complete
# ---------------------------------------------------------------------------

def test_claim_then_complete(conn, pool):
    queue.enqueue(pool, mac="m1", requested_by="op:dbahi", reason="manual",
                  switch="s", port="p", kind="host")
    req = queue.claim_next(pool)
    assert req["mac"] == "m1" and req["status"] == "claimed"
    queue.complete(pool, req["id"], outcome="host_ssh")
    row = conn.execute(
        "SELECT status, outcome FROM reconcile_requests WHERE id=%s",
        (req["id"],)).fetchone()
    assert row == ("done", "host_ssh")


# ---------------------------------------------------------------------------
# defer: cooldown backoff
# ---------------------------------------------------------------------------

def test_defer_increments_attempts_and_sets_cooldown(conn, pool):
    queue.enqueue(pool, mac="m2", requested_by="auto:mismatch",
                  reason="x", switch="s", port="p", kind="bmc")
    req = queue.claim_next(pool)
    queue.defer(pool, req["id"], cooldown_secs=60, max_attempts=3)
    row = conn.execute(
        "SELECT status, attempts, next_eligible > NOW() FROM reconcile_requests "
        "WHERE id=%s", (req["id"],)).fetchone()
    assert row[0] == "pending" and row[1] == 1 and row[2] is True


# ---------------------------------------------------------------------------
# defer: stuck cap
# ---------------------------------------------------------------------------

def test_defer_marks_stuck_at_max_attempts(conn, pool):
    queue.enqueue(pool, mac="m3", requested_by="auto:mismatch",
                  reason="x", switch="s", port="p", kind="bmc")
    req = queue.claim_next(pool)
    # simulate having already failed twice
    conn.execute("UPDATE reconcile_requests SET attempts=2 WHERE id=%s",
                 (req["id"],))
    queue.defer(pool, req["id"], cooldown_secs=60, max_attempts=3)
    status = conn.execute(
        "SELECT status FROM reconcile_requests WHERE id=%s", (req["id"],)).fetchone()[0]
    assert status == "stuck"


# ---------------------------------------------------------------------------
# claim skips cooled-down rows
# ---------------------------------------------------------------------------

def test_claim_skips_cooldowned_rows(conn, pool):
    queue.enqueue(pool, mac="m4", requested_by="auto:mismatch",
                  reason="x", switch="s", port="p", kind="bmc")
    conn.execute("UPDATE reconcile_requests "
                 "SET next_eligible = NOW() + INTERVAL '1 hour' "
                 "WHERE mac='m4'")
    assert queue.claim_next(pool) is None


# ---------------------------------------------------------------------------
# reclaim_stale_claims: DB-free assertion on the SQL shape it issues (status
# 'claimed' -> 'pending', claimed_at age gate via make_interval, rowcount
# return). A fake pool/conn/cursor captures the statement without a live DB so
# this asserts even when no Postgres is reachable.
# ---------------------------------------------------------------------------
class _FakeReclaimCursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeReclaimConn:
    def __init__(self, rowcount):
        self._rowcount = rowcount
        self.executed = []  # [(sql, params)]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeReclaimCursor(self._rowcount)


class _FakeReclaimCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


class _FakeReclaimPool:
    def __init__(self, rowcount):
        self.conn = _FakeReclaimConn(rowcount)

    def connection(self):
        return _FakeReclaimCtx(self.conn)


def test_reclaim_stale_claims_sql_shape_and_rowcount():
    pool = _FakeReclaimPool(13)
    n = queue.reclaim_stale_claims(pool, older_than_secs=180)
    assert n == 13  # returns the UPDATE rowcount
    sql, params = pool.conn.executed[0]
    # claimed -> pending, claimed_at cleared
    assert "SET status = 'pending'" in sql
    assert "claimed_at = NULL" in sql
    # only 'claimed' rows (NOT 'stuck')
    assert "WHERE status = 'claimed'" in sql
    # age gate uses the DB clock + make_interval(secs => %s) -- psycopg3 param
    assert "claimed_at < NOW() - make_interval(secs => %s)" in sql
    assert params == (180,)
