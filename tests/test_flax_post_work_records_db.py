"""DB-backed tests for the phase-4 work-record store (dut + work_records),
plus migration 033's rolling-retention policy function.

Skip convention copied from tests/test_flax_classify_desired_reservations.py.
THREE DSNs, each gating its tests:

  FLAX_TEST_POST_DSN    — the flax_post WRITER role (INSERT+SELECT only)
  FLAX_TEST_ADMIN_DSN   — superuser/owner, fixture cleanup ONLY (append-only
                          grants mean flax_post cannot delete its own rows)
  FLAX_TEST_CONTROL_DSN — flax_control (SELECT-only assertions)

SCRATCH DATABASE ONLY -- never point these DSNs at the live cluster. The
`test_retain_*` tests below call work_records_retain(..., dry_run=False),
which is FLEET-WIDE: it takes no DUT argument, so the fake TEST_PREFIX
scoping used elsewhere in this file cannot bound what a real run deletes.
On 2026-09-20 an earlier run of these exact tests was pointed at the live
database and deleted the fleet's work-record history with no way back.
They are therefore opt-in by construction, not by procedure: every one of
them takes the `destructive_ok` fixture, which hard-skips unless
FLAX_RETENTION_DESTRUCTIVE_OK=1 is set in the environment AND the writer
and admin connections are proven to be the same database AND both see a
categorical `_retention_scratch_marker` table that only the scratch
container has (see the fixture's own docstring for the full proof vs.
heuristic breakdown -- a row-count check is kept too, but only as a
secondary trip-wire).

Fixtures use FAKE identifiers only (def9633 rule); cleanup is prefix-scoped
to the fake TEST_PREFIX macs, which bounds the non-destructive tests above
but does NOT bound the fleet-wide retention tests below -- that is what
`destructive_ok` is for.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")

TEST_PREFIX = "aa:de:ad:04:"          # fake, phase-4-test-scoped mac prefix
P0_A = TEST_PREFIX + "00:01"
P0_B = TEST_PREFIX + "00:02"
SERIAL_A = "TESTSN-PHASE4-A"
SERIAL_B = "TESTSN-PHASE4-B"
KEYS = {"order": "TESTORD-004", "customer": "test-customer"}


def _env_dsn(name):
    d = os.environ.get(name)
    if not d:
        pytest.skip(name + " not set")
    return d


def _connect(dsn):
    try:
        return psycopg.connect(dsn, connect_timeout=3, autocommit=True)
    except Exception as exc:
        pytest.skip("no DB available: " + str(exc))


def _cleanup(admin):
    admin.execute(
        "DELETE FROM work_records WHERE dut_id IN "
        "(SELECT dut_id FROM dut WHERE p0_mac LIKE %s)", (TEST_PREFIX + "%",))
    admin.execute("DELETE FROM dut WHERE p0_mac LIKE %s", (TEST_PREFIX + "%",))
    admin.execute("DELETE FROM post_node WHERE bmc_mac LIKE 'aa:de:ad:04:%'")


@pytest.fixture()
def conns():
    """(writer_conn, admin_conn) + prefix-scoped cleanup around each test."""
    admin = _connect(_env_dsn("FLAX_TEST_ADMIN_DSN"))
    writer = _connect(_env_dsn("FLAX_TEST_POST_DSN"))
    try:
        _cleanup(admin)
        yield writer, admin
    finally:
        try:
            _cleanup(admin)
        except Exception:
            pass
        admin.close()
        writer.close()


@pytest.fixture()
def writer_pool():
    from psycopg_pool import ConnectionPool
    pool = ConnectionPool(_env_dsn("FLAX_TEST_POST_DSN"), min_size=1, max_size=2,
                          timeout=5.0, kwargs={"autocommit": True})
    yield pool
    pool.close()


def test_mint_idempotent_and_repair_mints_new(conns):
    from flax_post import records
    writer, _ = conns
    a1 = records.resolve_dut(writer, P0_A, SERIAL_A)
    a2 = records.resolve_dut(writer, P0_A, SERIAL_A)
    assert a1 == a2                                  # same pairing -> same dut
    b = records.resolve_dut(writer, P0_A, SERIAL_B)  # card moved to new chassis
    assert b != a1                                   # re-pairing mints NEW dut


def test_append_and_latest_payload_roundtrip(conns):
    from flax_post import records
    writer, _ = conns
    dut_id = records.resolve_dut(writer, P0_A, SERIAL_A)
    records.append_record(writer, dut_id, kind="inventory", keys=KEYS,
                          payload={"hash": "h1"})
    records.append_record(writer, dut_id, kind="inventory", keys=KEYS,
                          payload={"hash": "h2"})
    assert records.latest_payload(writer, dut_id, "inventory")["hash"] == "h2"
    assert records.latest_payload(writer, dut_id, "sel") is None


def test_record_observation_dedupes_and_deltas(conns, writer_pool):
    from flax_post import records
    writer, _ = conns
    fru = {"Product Name": "Test Platform", "Product Serial": SERIAL_A}
    sdr = {"CPU_Temp": {"value": "45", "status": "ok"}}
    sel1 = [{"id": "1", "ts": "07/01/2026 10:00:00", "event": "boot"}]
    kw = dict(p0_mac=P0_A, serial=SERIAL_A, fru=fru, sdr=sdr, keys=KEYS,
              pool=writer_pool)
    records.record_observation(sel=sel1, **kw)
    records.record_observation(sel=sel1, **kw)       # identical pass: no-op
    dut_id = records.resolve_dut(writer, P0_A, SERIAL_A)
    n_inv = writer.execute(
        "SELECT count(*) FROM work_records WHERE dut_id=%s AND kind='inventory'",
        (dut_id,)).fetchone()[0]
    n_sel = writer.execute(
        "SELECT count(*) FROM work_records WHERE dut_id=%s AND kind='sel'",
        (dut_id,)).fetchone()[0]
    assert (n_inv, n_sel) == (1, 1)                  # dedupe held
    # a new SEL entry -> exactly one more sel record carrying only the delta
    sel2 = sel1 + [{"id": "2", "ts": "07/02/2026 11:00:00", "event": "dimm ce"}]
    records.record_observation(sel=sel2, **kw)
    rows = writer.execute(
        "SELECT payload FROM work_records WHERE dut_id=%s AND kind='sel' "
        "ORDER BY id", (dut_id,)).fetchall()
    assert len(rows) == 2
    assert rows[1][0]["new"] == [sel2[1]]
    # real serial -> no identity-fault rows
    n_fault = writer.execute(
        "SELECT count(*) FROM work_records WHERE dut_id=%s AND kind='identity-fault'",
        (dut_id,)).fetchone()[0]
    assert n_fault == 0


def test_record_observation_identity_fault_once(conns, writer_pool):
    from flax_post import records
    kw = dict(p0_mac=P0_B, serial="To be defined by customer", fru={}, sdr={},
              sel=[], keys=KEYS, pool=writer_pool)
    records.record_observation(**kw)
    records.record_observation(**kw)                 # same fault: no repeat
    writer, _ = conns
    dut_id = records.resolve_dut(writer, P0_B, "To be defined by customer")
    n = writer.execute(
        "SELECT count(*) FROM work_records WHERE dut_id=%s AND kind='identity-fault'",
        (dut_id,)).fetchone()[0]
    assert n == 1


def test_append_only_is_grant_enforced(conns):
    """THE phase gate assertion: the writer role cannot UPDATE or DELETE."""
    from flax_post import records
    writer, _ = conns
    dut_id = records.resolve_dut(writer, P0_A, SERIAL_A)
    rec_id = records.append_record(writer, dut_id, kind="inventory",
                                   payload={"hash": "x"})
    for sql, params in (
        ("UPDATE work_records SET payload = '{}' WHERE id = %s", (rec_id,)),
        ("DELETE FROM work_records WHERE id = %s", (rec_id,)),
        ("UPDATE dut SET serial = 'nope' WHERE dut_id = %s", (dut_id,)),
        ("DELETE FROM dut WHERE dut_id = %s", (dut_id,)),
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute(sql, params)


def test_dut_current_view_recency(conns):
    from flax_post import records
    writer, _ = conns
    a = records.resolve_dut(writer, P0_A, SERIAL_A)   # old assembly
    b = records.resolve_dut(writer, P0_A, SERIAL_B)   # newer assembly, same card
    records.append_record(writer, a, kind="inventory", payload={"hash": "old"})
    records.append_record(writer, b, kind="inventory", payload={"hash": "new"})
    rows = writer.execute(
        "SELECT dut_id FROM dut_current WHERE p0_mac = %s "
        "ORDER BY last_record_at DESC NULLS LAST, first_seen DESC",
        (P0_A,)).fetchall()
    assert rows[0][0] == b                            # most-recent assembly first


def test_record_flash_dedupes_unchanged_outcome_and_records_changes(conns, writer_pool):
    from flax_post import records
    writer, _ = conns
    kw = dict(p0_mac=P0_A, serial=SERIAL_A, port="et1", keys=KEYS,
              pool=writer_pool)
    row1 = {"current_version": "1.0.6", "target_version": "1.0.7",
            "fault_reason": None, "percent": 100}
    records.record_flash(terminal="up_to_date", row=row1, timeline=[{"t": 1}], **kw)
    records.record_flash(terminal="up_to_date", row=row1, timeline=[{"t": 2}], **kw)
    dut_id = records.resolve_dut(writer, P0_A, SERIAL_A)
    n = writer.execute(
        "SELECT count(*) FROM work_records WHERE dut_id=%s AND kind='fw-flash'",
        (dut_id,)).fetchone()[0]
    assert n == 1                                    # identical outcome: no repeat
    row2 = dict(row1, fault_reason="activation-timeout")
    records.record_flash(terminal="fault", row=row2, timeline=[{"t": 3}], **kw)
    rows = writer.execute(
        "SELECT payload FROM work_records WHERE dut_id=%s AND kind='fw-flash' "
        "ORDER BY id", (dut_id,)).fetchall()
    assert len(rows) == 2                             # outcome changed: new record
    assert rows[1][0]["terminal"] == "fault"
    assert rows[1][0]["fault_reason"] == "activation-timeout"


def test_node_identity_and_record_action_roundtrip(conns, writer_pool):
    from flax_post import records
    _, admin = conns
    bmc_mac = "aa:de:ad:04:bb:01"
    host_mac = "aa:de:ad:04:00:01"
    serial = "TESTSN-PHASE4-A"
    admin.execute(
        "INSERT INTO post_node (bmc_mac, serial, host_mac, order_no, "
        "last_switch, last_port) VALUES (%s, %s, %s, %s, %s, %s)",
        (bmc_mac, serial, host_mac, "TESTORD-004", "rabbit-test-p4", "et1"))
    assert records.node_identity(bmc_mac, pool=writer_pool) == (host_mac, serial)
    assert records.node_identity("aa:de:ad:04:bb:99", pool=writer_pool) == (None, None)

    records.record_action(p0_mac=host_mac, serial=serial, port="et1",
                          action="power-on", ok=True, detail="ipmi ok",
                          keys=KEYS, pool=writer_pool)
    writer, _ = conns
    dut_id = records.resolve_dut(writer, host_mac, serial)
    rows = writer.execute(
        "SELECT payload FROM work_records WHERE dut_id=%s AND kind='fw-action'",
        (dut_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == {"port": "et1", "action": "power-on", "ok": True,
                          "detail": "ipmi ok"}


def test_control_role_is_read_only(conns):
    """flax_control: SELECT works on tables + view; INSERT denied."""
    from flax_post import records
    writer, _ = conns
    records.resolve_dut(writer, P0_A, SERIAL_A)
    control = _connect(_env_dsn("FLAX_TEST_CONTROL_DSN"))
    try:
        assert control.execute(
            "SELECT count(*) FROM dut WHERE p0_mac LIKE %s",
            (TEST_PREFIX + "%",)).fetchone()[0] >= 1
        control.execute("SELECT count(*) FROM work_records").fetchone()
        control.execute("SELECT count(*) FROM dut_current").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            control.execute(
                "INSERT INTO dut (p0_mac, serial) VALUES (%s, %s)",
                (TEST_PREFIX + "ff:ff", "NOPE"))
    finally:
        control.close()


# --- migration 033: rolling retention policy function -----------------------
# Spec: docs/superpowers/specs/2026-09-19-work-records-rolling-retention-design.md

def _retain(conn, keep=5, dry_run=True, max_deletes=20000):
    """Call the policy function; returns {action: n}."""
    rows = conn.execute(
        "SELECT action, n FROM work_records_retain(%s, %s, %s)",
        (keep, dry_run, max_deletes)).fetchall()
    return {a: n for a, n in rows}


def _mk(admin, p0_mac, serial, kind, n, keys=None, start=1, base_minutes=None):
    """n records for (p0_mac, serial) of `kind`, one per minute, the oldest
    `base_minutes` ago (default: n). Pass base_minutes explicitly whenever a
    test compares two DUTs' recency -- equal timestamps make "newer" a tie."""
    base = n if base_minutes is None else base_minutes
    admin.execute("INSERT INTO dut (p0_mac, serial) VALUES (%s, %s) "
                  "ON CONFLICT (p0_mac, serial) DO NOTHING", (p0_mac, serial))
    dut_id = admin.execute("SELECT dut_id FROM dut WHERE p0_mac=%s AND serial=%s",
                           (p0_mac, serial)).fetchone()[0]
    for idx, i in enumerate(range(start, start + n)):
        admin.execute(
            "INSERT INTO work_records (dut_id, owner_role, kind, keys, payload, at) "
            "VALUES (%s, 'post', %s, %s::jsonb, %s::jsonb, now() - (%s || ' minutes')::interval)",
            (dut_id, kind, __import__("json").dumps(keys or KEYS),
             __import__("json").dumps({"i": i}), base - idx))
    return dut_id


def _ids(admin, dut_id):
    return [r[0] for r in admin.execute(
        "SELECT id FROM work_records WHERE dut_id=%s ORDER BY at, id", (dut_id,)).fetchall()]


_DESTRUCTIVE_ROW_LIMIT = 200
_SCRATCH_MARKER_TABLE = "_retention_scratch_marker"

_SAME_DB_QUERY = (
    "SELECT (pg_control_system()).system_identifier, inet_server_addr(), "
    "inet_server_port(), current_database()"
)


def _same_database(conn_a, conn_b):
    """True iff two connections are provably talking to the SAME database:
    same cluster (pg_control_system().system_identifier, which is unique
    per initdb) AND the same database within it (inet_server_addr(),
    inet_server_port(), current_database()). This is a proof, not a
    heuristic -- two different clusters or databases cannot produce a
    matching tuple by accident."""
    return conn_a.execute(_SAME_DB_QUERY).fetchone() == \
        conn_b.execute(_SAME_DB_QUERY).fetchone()


def _has_scratch_marker(conn):
    return conn.execute(
        "SELECT to_regclass(%s)", (_SCRATCH_MARKER_TABLE,)).fetchone()[0] is not None


@pytest.fixture()
def destructive_ok(conns):
    """Hard-skip guard: every test below that calls _retain(..., dry_run=False)
    takes this fixture. work_records_retain is fleet-wide (no DUT argument),
    so TEST_PREFIX scoping cannot bound it -- this is the guard that stops a
    repeat of the 2026-09-20 incident (see the file docstring), by
    construction rather than by the operator remembering to point at the
    right DSN.

    What is a PROOF here vs. a HEURISTIC (the 2026-09-20 re-review found the
    previous version of this comment read more confidently than the old
    row-count-only mechanism actually guaranteed -- it does not, this time):

      PROOF 1 -- same database. The writer (FLAX_TEST_POST_DSN) and admin
      (FLAX_TEST_ADMIN_DSN) connections are compared on
      pg_control_system().system_identifier (unique per cluster),
      inet_server_addr(), inet_server_port(), and current_database(). If
      they disagree, we skip. This closes the wrong-connection gap flagged
      in re-review: work_records_retain is SECURITY DEFINER, so
      test_retain_deletes_as_the_writer_role_through_security_definer's
      real delete runs over FLAX_TEST_POST_DSN (the writer), not
      FLAX_TEST_ADMIN_DSN -- checking only the admin connection (as before)
      would validate the wrong target if the two DSNs ever pointed at
      different databases.

      PROOF 2 -- categorical scratch marker. Both connections must see a
      table named `_retention_scratch_marker`. Nothing creates this table
      on a live database; its presence is a categorical, not statistical,
      signal that the target is the disposable scratch container. It is
      checked on BOTH connections (not just admin) because PROOF 1 already
      established they're the same database, but checking both directly
      means this proof does not depend on PROOF 1 to be meaningful. If the
      scratch container is ever recreated, the marker must be recreated
      too (see docs/superpowers/plans/2026-09-19-work-records-rolling-
      retention.md, Global Constraints).

      HEURISTIC -- row count. work_records, seen through admin, must hold
      fewer than _DESTRUCTIVE_ROW_LIMIT rows. This is kept as an extra
      trip-wire but it is NOT the safety property: it is a row-count proxy
      that a growing scratch fixture could exceed, and retention's own
      purpose is to shrink live counts over time, so a future live count
      under the limit would not by itself make live safe. The two proofs
      above are what actually gate destructive execution.

    Skips unless ALL of the following hold:
      1. FLAX_RETENTION_DESTRUCTIVE_OK=1 is set (explicit opt-in).
      2. PROOF 1: writer and admin are the same database.
      3. PROOF 2: `_retention_scratch_marker` exists, seen through BOTH
         connections.
      4. HEURISTIC: work_records (via admin) holds < _DESTRUCTIVE_ROW_LIMIT
         rows.
    """
    if os.environ.get("FLAX_RETENTION_DESTRUCTIVE_OK") != "1":
        pytest.skip(
            "FLAX_RETENTION_DESTRUCTIVE_OK=1 not set -- refusing to run a "
            "fleet-wide dry_run=False retention test (see this file's "
            "docstring); set it only against the scratch DB")
    writer, admin = conns
    if not _same_database(writer, admin):
        pytest.skip(
            "FLAX_TEST_POST_DSN (writer) and FLAX_TEST_ADMIN_DSN (admin) do not "
            "provably point at the same database (system_identifier / "
            "inet_server_addr / inet_server_port / current_database "
            "differ) -- refusing to run a fleet-wide dry_run=False "
            "retention test: the writer connection is the one the "
            "SECURITY DEFINER delete actually runs through, and it must "
            "be proven identical to the connection the other checks below "
            "validate")
    for label, conn in (("admin", admin), ("writer", writer)):
        if not _has_scratch_marker(conn):
            pytest.skip(
                "{} table not found via the {} connection -- refusing to "
                "run a fleet-wide dry_run=False retention test; this "
                "table is the categorical proof the target is the "
                "disposable scratch container, not a live database "
                "(recreate it if the scratch container was rebuilt: see "
                "docs/superpowers/plans/2026-09-19-work-records-rolling-"
                "retention.md)".format(_SCRATCH_MARKER_TABLE, label))
    n = admin.execute("SELECT count(*) FROM work_records").fetchone()[0]
    if n >= _DESTRUCTIVE_ROW_LIMIT:
        pytest.skip(
            "work_records holds {} rows (>= {}) -- this looks like a "
            "populated, possibly LIVE database, not the empty scratch "
            "container; refusing to run a fleet-wide dry_run=False "
            "retention test against it".format(n, _DESTRUCTIVE_ROW_LIMIT))


def test_retain_keeps_newest_five_plus_birth(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 12)
    before = _ids(admin, dut_id)
    got = _retain(admin, dry_run=False)
    assert got["records"] == 6                      # 12 - (5 newest + 1 birth)
    after = _ids(admin, dut_id)
    assert after == [before[0]] + before[-5:]       # birth + newest 5


def test_retain_is_per_keys_group(conns, destructive_ok):
    writer, admin = conns
    other = {"order": "TESTORD-005", "customer": "test-customer"}
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 8, base_minutes=100)
    _mk(admin, P0_A, SERIAL_A, "inventory", 8, keys=other, start=100, base_minutes=50)
    _retain(admin, dry_run=False)
    n_default = admin.execute(
        "SELECT count(*) FROM work_records WHERE dut_id=%s AND keys=%s::jsonb",
        (dut_id, __import__("json").dumps(KEYS))).fetchone()[0]
    n_other = admin.execute(
        "SELECT count(*) FROM work_records WHERE dut_id=%s AND keys=%s::jsonb",
        (dut_id, __import__("json").dumps(other))).fetchone()[0]
    # The birth pin is per (dut, kind), so it lands in the older group only.
    assert (n_default, n_other) == (6, 5)


def test_retain_is_per_kind(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 8, base_minutes=100)
    _mk(admin, P0_A, SERIAL_A, "sel", 8, start=100, base_minutes=100)
    _retain(admin, dry_run=False)
    per_kind = dict(admin.execute(
        "SELECT kind, count(*) FROM work_records WHERE dut_id=%s GROUP BY 1",
        (dut_id,)).fetchall())
    assert per_kind == {"inventory": 6, "sel": 6}


def test_retain_dry_run_deletes_nothing_and_predicts_the_real_run(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 12)
    dry = _retain(admin, dry_run=True)
    assert len(_ids(admin, dut_id)) == 12
    wet = _retain(admin, dry_run=False)
    assert dry["records"] == wet["records"] == 6


def test_retain_under_the_keep_count_is_a_no_op(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 3)
    assert _retain(admin, dry_run=False)["records"] == 0
    assert len(_ids(admin, dut_id)) == 3


def test_retain_honours_max_deletes_and_reports_the_remainder(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 20)
    first = _retain(admin, dry_run=False, max_deletes=5)
    assert (first["records"], first["capped_remaining"]) == (5, 9)   # 14 victims, 5 done
    second = _retain(admin, dry_run=False, max_deletes=100)
    assert second["records"] == 9
    assert len(_ids(admin, dut_id)) == 6


def test_retain_deletes_oldest_first(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 12)
    before = _ids(admin, dut_id)
    _retain(admin, dry_run=False, max_deletes=2)
    after = _ids(admin, dut_id)
    assert before[1:3] == [i for i in before if i not in after]   # birth survives, next two go


def test_retain_grants(conns):
    writer, admin = conns
    _mk(admin, P0_A, SERIAL_A, "inventory", 8)
    assert _retain(writer, dry_run=True)["records"] == 2          # flax_post may EXECUTE
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        writer.execute("DELETE FROM work_records WHERE dut_id = -1")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        writer.execute("DELETE FROM dut WHERE dut_id = -1")


def test_retain_not_executable_by_flax_control(conns):
    control = _connect(_env_dsn("FLAX_TEST_CONTROL_DSN"))
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            control.execute("SELECT * FROM work_records_retain(5, true, 10)")
    finally:
        control.close()


def test_retain_deletes_as_the_writer_role_through_security_definer(conns, destructive_ok):
    # Settling test for SECURITY DEFINER: flax_post holds no DELETE grant on
    # work_records, so a real deletion driven over the writer connection only
    # succeeds because the function runs as its owner (postgres).
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 8)
    got = _retain(writer, dry_run=False)
    assert got["records"] == 2
    assert len(_ids(admin, dut_id)) == 6


def test_retain_empty_serial_has_no_birth_pin(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, "", "inventory", 9)       # serial '' = identity unknown
    before = _ids(admin, dut_id)
    got = _retain(admin, dry_run=False)
    assert got["records"] == 4                          # 9 - 5, birth NOT pinned
    assert _ids(admin, dut_id) == before[-5:]


def test_retain_empty_serial_cleared_once_a_real_serial_is_newer(conns, destructive_ok):
    writer, admin = conns
    empty_id = _mk(admin, P0_A, "", "identity-fault", 3, base_minutes=100)   # older
    real_id = _mk(admin, P0_A, SERIAL_A, "inventory", 2, start=100, base_minutes=10)
    got = _retain(admin, dry_run=False)
    assert got["empty_serial_records"] == 3
    assert _ids(admin, empty_id) == []
    assert len(_ids(admin, real_id)) == 2
    assert admin.execute("SELECT count(*) FROM dut WHERE dut_id=%s",
                         (empty_id,)).fetchone()[0] == 0     # emptied row goes too


def test_retain_empty_serial_kept_while_not_superseded(conns, destructive_ok):
    writer, admin = conns
    real_id = _mk(admin, P0_A, SERIAL_A, "inventory", 2, start=100, base_minutes=100)
    empty_id = _mk(admin, P0_A, "", "identity-fault", 3, base_minutes=10)    # newer
    got = _retain(admin, dry_run=False)
    assert got["empty_serial_records"] == 0
    assert len(_ids(admin, empty_id)) == 3


def test_retain_empty_serial_on_another_nic_is_not_superseded(conns, destructive_ok):
    writer, admin = conns
    empty_id = _mk(admin, P0_A, "", "identity-fault", 2, base_minutes=100)
    _mk(admin, P0_B, SERIAL_B, "inventory", 2, start=100, base_minutes=10)  # other NIC
    assert _retain(admin, dry_run=False)["empty_serial_records"] == 0
    assert len(_ids(admin, empty_id)) == 2


def test_retain_deletes_record_less_dut_older_than_a_day(conns, destructive_ok):
    writer, admin = conns
    admin.execute("INSERT INTO dut (p0_mac, serial, first_seen) "
                  "VALUES (%s, %s, now() - interval '25 hours')", (P0_B, SERIAL_B))
    got = _retain(admin, dry_run=False)
    assert got["dut_rows"] == 1
    assert admin.execute("SELECT count(*) FROM dut WHERE p0_mac=%s", (P0_B,)).fetchone()[0] == 0


def test_retain_keeps_a_freshly_minted_record_less_dut(conns, destructive_ok):
    writer, admin = conns
    admin.execute("INSERT INTO dut (p0_mac, serial) VALUES (%s, %s)", (P0_B, SERIAL_B))
    assert _retain(admin, dry_run=False)["dut_rows"] == 0
    assert admin.execute("SELECT count(*) FROM dut WHERE p0_mac=%s", (P0_B,)).fetchone()[0] == 1


def test_retain_dry_run_matches_the_real_run_when_the_cap_splits_a_superseded_dut(conns, destructive_ok):
    """A capped run leaves some of a superseded DUT's records behind, so its
    row survives — and the dry run must report that, not a row it won't delete."""
    writer, admin = conns
    empty_id = _mk(admin, P0_A, "", "identity-fault", 4, base_minutes=100)
    _mk(admin, P0_A, SERIAL_A, "inventory", 2, start=100, base_minutes=10)
    dry = _retain(admin, dry_run=True, max_deletes=2)
    wet = _retain(admin, dry_run=False, max_deletes=2)
    assert dry["empty_serial_records"] == wet["empty_serial_records"] == 2
    assert dry["dut_rows"] == wet["dut_rows"] == 0      # 2 of its 4 records remain
    assert len(_ids(admin, empty_id)) == 2


def test_retain_dry_run_reports_empty_serial_and_dut_rows_without_deleting(conns):
    writer, admin = conns
    empty_id = _mk(admin, P0_A, "", "identity-fault", 3, base_minutes=100)
    _mk(admin, P0_A, SERIAL_A, "inventory", 2, start=100, base_minutes=10)
    admin.execute("INSERT INTO dut (p0_mac, serial, first_seen) "
                  "VALUES (%s, %s, now() - interval '25 hours')", (P0_B, SERIAL_B))
    dry = _retain(admin, dry_run=True)
    # 2 dut rows: the 25h-old record-less one, and the superseded empty-serial
    # one whose records all go (spec D4 deletes that row with them).
    assert (dry["empty_serial_records"], dry["dut_rows"]) == (3, 2)
    assert len(_ids(admin, empty_id)) == 3
    assert admin.execute("SELECT count(*) FROM dut WHERE p0_mac=%s", (P0_B,)).fetchone()[0] == 1


# --- I1: a NULL argument must not turn the cap (or the run) off -------------
# LIMIT NULL means NO LIMIT: a NULL max_deletes would delete every victim
# uncapped, and a NULL keep/dry_run is equally a mistyped operator call, not
# a legitimate "no limit" request. Both must raise, never silently delete.

def test_retain_rejects_null_max_deletes(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 8)
    with pytest.raises(psycopg.errors.RaiseException):
        _retain(admin, keep=5, dry_run=False, max_deletes=None)
    assert len(_ids(admin, dut_id)) == 8          # nothing deleted


def test_retain_rejects_null_keep(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 8)
    with pytest.raises(psycopg.errors.RaiseException):
        _retain(admin, keep=None, dry_run=False, max_deletes=100)
    assert len(_ids(admin, dut_id)) == 8          # nothing deleted


def test_retain_rejects_null_dry_run(conns, destructive_ok):
    writer, admin = conns
    dut_id = _mk(admin, P0_A, SERIAL_A, "inventory", 8)
    with pytest.raises(psycopg.errors.RaiseException):
        _retain(admin, keep=5, dry_run=None, max_deletes=100)
    assert len(_ids(admin, dut_id)) == 8          # nothing deleted


# --- I5(b): sel delta baselining still works after a sweep ------------------
# Spec §3 worked cases / §6: "sel records: newest 5 keep full snapshots; the
# delta baseline is the latest record, so deltas still work." record_observation
# diffs a new SEL list against the newest 'sel' record's full snapshot
# (records.record_observation), so if a sweep ever left the surviving newest
# record holding only a delta, the next real delta would be computed against
# the wrong baseline and silently under-report.

def test_retain_sweep_leaves_sel_delta_baseline_intact(conns, writer_pool, destructive_ok):
    from flax_post import records
    writer, admin = conns
    # Seed more than `keep` sel records the old way (full snapshots), so the
    # sweep has victims to remove, then let record_observation append one
    # more real delta-style record on top.
    full_snapshot = [{"id": str(i), "ts": "07/01/2026 10:00:00", "event": "boot"}
                     for i in range(3)]
    dut_id = _mk(admin, P0_A, SERIAL_A, "sel", 9)
    for rec_id in _ids(admin, dut_id):
        admin.execute("UPDATE work_records SET payload = %s::jsonb WHERE id = %s",
                     (__import__("json").dumps({"entries": full_snapshot}), rec_id))
    got = _retain(admin, dry_run=False)
    assert got["records"] == 3                    # 9 - (5 newest + 1 birth pin)
    surviving = _ids(admin, dut_id)
    assert len(surviving) == 6
    payload = admin.execute("SELECT payload FROM work_records WHERE id = %s",
                            (surviving[-1],)).fetchone()[0]
    assert payload["entries"] == full_snapshot     # full snapshot survives, not a delta
    # a real record_observation delta call baselines correctly against it
    fru = {"Product Name": "Test Platform", "Product Serial": SERIAL_A}
    new_entry = {"id": "99", "ts": "07/02/2026 11:00:00", "event": "dimm ce"}
    records.record_observation(p0_mac=P0_A, serial=SERIAL_A, fru=fru, sdr={},
                               sel=full_snapshot + [new_entry], keys=KEYS,
                               pool=writer_pool)
    rows = writer.execute(
        "SELECT payload FROM work_records WHERE dut_id=%s AND kind='sel' "
        "ORDER BY id", (dut_id,)).fetchall()
    assert rows[-1][0]["new"] == [new_entry]       # delta computed against the full snapshot
