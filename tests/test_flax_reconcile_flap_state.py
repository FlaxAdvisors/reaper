"""Per-MAC convergence circuit-breaker (Task 2).

Two layers, both DB-free:

  * `db.flap_blocked` is a pure predicate over a crafted row + a reference
    `now` (the DB clock from `db.db_now`). Unit-tested directly with synthetic
    timestamps -- no Postgres needed.

  * The cycle's *gating* logic (enqueue-skip when blocked, record_flap only for
    auto successes not operator requests, clear_flap_state for converged macs)
    is exercised by stubbing the db/queue boundary on the cycle module so the
    Reconciler runs without a live database. (The real-DB end-to-end behaviour
    of record_flap -- threshold -> backoff -> faulted-once -- is covered by the
    skip-gated integration tests in test_flax_reconcile_cycle.py when a
    Postgres is present; here we assert the orchestration contract.)
"""
import datetime

import pytest

from flax_reconcile import db
from flax_reconcile.cycle import Reconciler


UTC = datetime.timezone.utc


def _ts(**kw):
    return datetime.datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC) + datetime.timedelta(**kw)


# ---------------------------------------------------------------------------
# db.flap_blocked: pure predicate
# ---------------------------------------------------------------------------
def test_flap_blocked_during_cooldown_after_success():
    now = _ts()
    row = {"last_flap_at": now - datetime.timedelta(seconds=10),
           "backoff_until": None}
    # flapped 10s ago, cooldown 60s -> still blocked
    assert db.flap_blocked(row, now, kick_cooldown_secs=60) is True


def test_flap_blocked_clears_after_cooldown_elapses():
    now = _ts()
    row = {"last_flap_at": now - datetime.timedelta(seconds=61),
           "backoff_until": None}
    # flapped 61s ago, cooldown 60s -> unblocked
    assert db.flap_blocked(row, now, kick_cooldown_secs=60) is False


def test_flap_blocked_during_backoff_window():
    now = _ts()
    row = {"last_flap_at": now - datetime.timedelta(seconds=5000),
           "backoff_until": now + datetime.timedelta(seconds=300)}
    # cooldown long past, but backoff is still in the future -> blocked
    assert db.flap_blocked(row, now, kick_cooldown_secs=60) is True


def test_flap_blocked_after_backoff_expired_and_cooldown_past():
    now = _ts()
    row = {"last_flap_at": now - datetime.timedelta(seconds=5000),
           "backoff_until": now - datetime.timedelta(seconds=1)}
    assert db.flap_blocked(row, now, kick_cooldown_secs=60) is False


def test_flap_blocked_empty_row_is_not_blocked():
    now = _ts()
    assert db.flap_blocked({}, now, kick_cooldown_secs=60) is False


# ---------------------------------------------------------------------------
# db._flap_decision: pure circuit-breaker decision (re-arm vs newly-faulted).
#
# record_flap's full UPSERT is only exercised against a live Postgres (the
# skip-gated integration path in test_flax_reconcile_cycle.py). The re-arm
# boundary -- backoff must be re-armed on EVERY threshold hit, but the fault
# emitted only once -- is the correctness-critical bit, so we unit-test the
# extracted decision DB-free here.
# ---------------------------------------------------------------------------
def test_flap_decision_below_threshold_no_rearm_no_fault():
    rearm, newly = db._flap_decision(2, False, threshold=3)
    assert rearm is False
    assert newly is False


def test_flap_decision_first_threshold_hit_rearms_and_faults():
    # false -> true edge: arm backoff AND emit the fault.
    rearm, newly = db._flap_decision(3, False, threshold=3)
    assert rearm is True
    assert newly is True


def test_flap_decision_rehit_after_faulted_rearms_but_no_new_fault():
    # The bug fix: a MAC that re-hits threshold while already faulted must
    # re-arm backoff (rearm True) but must NOT report newly-faulted again.
    rearm, newly = db._flap_decision(3, True, threshold=3)
    assert rearm is True
    assert newly is False


def test_flap_decision_over_threshold_already_faulted_rearms_no_fault():
    rearm, newly = db._flap_decision(7, True, threshold=3)
    assert rearm is True
    assert newly is False


# ---------------------------------------------------------------------------
# Cycle gating: stub the db/queue boundary so the Reconciler runs DB-free.
# ---------------------------------------------------------------------------
class _Stub:
    """Records db.record_flap / db.clear_flap_state calls and serves a scripted
    flap_state + mismatch/location/queue surface to run_one_cycle DB-free."""

    def __init__(self, *, mismatches, flap_state, now, locations,
                 record_flap_newly=False):
        self.mismatches = mismatches
        self.flap_state = flap_state
        self.now = now
        self.locations = locations
        self.record_flap_newly = record_flap_newly
        self.record_flap_calls = []
        self.clear_flap_calls = []
        self.clear_stale_calls = []
        self.release_calls = []
        self.reclaim_calls = []
        self.reclaimed = 0
        self.log_actions = []
        self.enqueued = []
        self._claim_queue = []
        # Shared ordering log so a test can assert release-before-flap.
        self.events = []
        self.release_result = []

    # -- read surface (PASS 1 is made a no-op: no steers) --
    def read_switch_facts_ports(self, pool):
        return {}

    def compute_steers(self, desired, sf, no_steer):
        return []

    def read_desired_ports(self, pool):
        return []

    def read_active_leases(self, pool):
        return []

    def read_reservations(self, pool, eligible_sources=None):
        self.read_reservations_eligible_sources = eligible_sources
        return []

    def compute_mismatches(self, leases, hosts):
        return self.mismatches

    def read_flap_state(self, pool):
        return dict(self.flap_state)

    def db_now(self, pool):
        return self.now

    def resolve_location(self, pool, mac):
        return self.locations.get(mac, {})

    def port_link_up(self, fact):
        return True

    # -- record/clear (assertions) --
    def clear_flap_state(self, pool, macs):
        self.clear_flap_calls.append(list(macs))

    def clear_stale_flap_state(self, pool, *, older_than_secs):
        self.clear_stale_calls.append(older_than_secs)

    def record_flap(self, pool, mac, *, threshold, window_secs, backoff_secs):
        self.record_flap_calls.append(mac)
        return self.record_flap_newly

    def release_stale_lease(self, pool, mac):
        self.release_calls.append(mac)
        self.events.append(("release", mac))
        return list(self.release_result)

    # -- queue surface --
    def enqueue(self, pool, *, mac, requested_by, reason, switch=None,
                port=None, kind=None):
        self.enqueued.append({"mac": mac, "requested_by": requested_by,
                              "reason": reason, "switch": switch,
                              "port": port, "kind": kind})
        # The enqueued auto request becomes claimable for the kick loop.
        self._claim_queue.append(
            {"id": len(self.enqueued), "mac": mac, "switch": switch,
             "port": port, "kind": kind, "reason": reason, "attempts": 0,
             "status": "pending"})
        return True

    def reclaim_stale_claims(self, pool, *, older_than_secs):
        self.reclaim_calls.append(older_than_secs)
        return self.reclaimed

    def seed_claim(self, req):
        self._claim_queue.append(req)

    def claim_next(self, pool):
        return self._claim_queue.pop(0) if self._claim_queue else None

    def complete(self, pool, req_id, *, outcome):
        pass

    def defer(self, pool, req_id, *, cooldown_secs, max_attempts):
        pass

    def log_action(self, pool, **kw):
        self.log_actions.append(kw)

    def emit_circuit_fault(self, pool, req):
        pass


def _patch_cycle(monkeypatch, stub):
    import flax_reconcile.cycle as cyc
    monkeypatch.setattr(cyc.db, "read_switch_facts_ports", stub.read_switch_facts_ports)
    monkeypatch.setattr(cyc.steer, "compute_steers", stub.compute_steers)
    monkeypatch.setattr(cyc.db, "read_desired_ports", stub.read_desired_ports)
    monkeypatch.setattr(cyc.db, "read_active_leases", stub.read_active_leases)
    monkeypatch.setattr(cyc.db, "read_reservations", stub.read_reservations)
    monkeypatch.setattr(cyc.mismatch, "compute_mismatches", stub.compute_mismatches)
    monkeypatch.setattr(cyc.db, "read_flap_state", stub.read_flap_state)
    monkeypatch.setattr(cyc.db, "db_now", stub.db_now)
    monkeypatch.setattr(cyc.db, "resolve_location", stub.resolve_location)
    monkeypatch.setattr(cyc.db, "port_link_up", stub.port_link_up)
    monkeypatch.setattr(cyc.db, "clear_flap_state", stub.clear_flap_state)
    monkeypatch.setattr(cyc.db, "clear_stale_flap_state", stub.clear_stale_flap_state)
    monkeypatch.setattr(cyc.db, "record_flap", stub.record_flap)
    monkeypatch.setattr(cyc.db, "release_stale_lease", stub.release_stale_lease)
    monkeypatch.setattr(cyc.queue, "reclaim_stale_claims", stub.reclaim_stale_claims)
    monkeypatch.setattr(cyc.queue, "enqueue", stub.enqueue)
    monkeypatch.setattr(cyc.queue, "claim_next", stub.claim_next)
    monkeypatch.setattr(cyc.queue, "complete", stub.complete)
    monkeypatch.setattr(cyc.queue, "defer", stub.defer)
    monkeypatch.setattr(cyc.actions, "log_action", stub.log_action)
    monkeypatch.setattr(cyc, "_emit_circuit_fault", stub.emit_circuit_fault)


def _reconciler(ladder_result, eligible_sources=None):
    from tests.test_flax_reconcile_cycle import FakeLadder
    return Reconciler(switches={}, config={"flap_hold_seconds": 2},
                      obmc_user="u", obmc_pass="p", host_creds=[],
                      ladder=FakeLadder(ladder_result),
                      eligible_sources=eligible_sources)


# (a) a mac blocked by cooldown is NOT enqueued; an unblocked mac IS.
def test_cooldown_blocked_mac_not_enqueued_unblocked_is(monkeypatch):
    now = _ts()
    blocked = "aa:bb:cc:00:00:01"
    free = "aa:bb:cc:00:00:02"
    stub = _Stub(
        mismatches=[{"mac": blocked, "lease_ip": None, "reserved_ip": "1.1.1.1"},
                    {"mac": free, "lease_ip": None, "reserved_ip": "1.1.1.2"}],
        flap_state={blocked: {"last_flap_at": now - datetime.timedelta(seconds=10),
                              "backoff_until": None, "faulted": False}},
        now=now,
        locations={blocked: {"switch": "s", "port": "p1", "kind": "bmc"},
                   free: {"switch": "s", "port": "p2", "kind": "bmc"}})
    _patch_cycle(monkeypatch, stub)
    summary = _reconciler(("switch_flap", True)).run_one_cycle(pool=object())
    macs = [e["mac"] for e in stub.enqueued]
    assert blocked not in macs
    assert free in macs
    assert summary["enqueued"] == 1


# (a2) Reconciler.eligible_sources (registry capability lookup, Task 1) is
# threaded through to db.read_reservations unchanged -- both the frozenset
# mode and the None (legacy-fallback) default.
def test_eligible_sources_threaded_into_read_reservations(monkeypatch):
    stub = _Stub(mismatches=[], flap_state={}, now=_ts(), locations={})
    _patch_cycle(monkeypatch, stub)
    sources = frozenset({"triage", "legacy-import", ""})
    _reconciler(("switch_flap", True), eligible_sources=sources).run_one_cycle(
        pool=object())
    assert stub.read_reservations_eligible_sources == sources


def test_eligible_sources_defaults_to_none(monkeypatch):
    stub = _Stub(mismatches=[], flap_state={}, now=_ts(), locations={})
    _patch_cycle(monkeypatch, stub)
    _reconciler(("switch_flap", True)).run_one_cycle(pool=object())
    assert stub.read_reservations_eligible_sources is None


# (b) a mac blocked by backoff_until is NOT enqueued (and counted circuit_open).
def test_backoff_blocked_mac_not_enqueued(monkeypatch):
    now = _ts()
    mac = "aa:bb:cc:00:00:03"
    stub = _Stub(
        mismatches=[{"mac": mac, "lease_ip": None, "reserved_ip": "1.1.1.3"}],
        flap_state={mac: {"last_flap_at": now - datetime.timedelta(seconds=9999),
                          "backoff_until": now + datetime.timedelta(seconds=300),
                          "faulted": True}},
        now=now,
        locations={mac: {"switch": "s", "port": "p3", "kind": "bmc"}})
    _patch_cycle(monkeypatch, stub)
    summary = _reconciler(("switch_flap", True)).run_one_cycle(pool=object())
    assert stub.enqueued == []
    assert summary["enqueued"] == 0
    assert summary["circuit_open"] == 1


# (c) record_flap is called for an auto-mismatch successful flap, NOT for an
#     operator bmc-reset.
def test_record_flap_for_auto_success_not_operator(monkeypatch):
    now = _ts()
    auto_mac = "aa:bb:cc:00:00:04"
    op_mac = "aa:bb:cc:00:00:05"
    stub = _Stub(
        mismatches=[{"mac": auto_mac, "lease_ip": None, "reserved_ip": "1.1.1.4"}],
        flap_state={},
        now=now,
        locations={auto_mac: {"switch": "s", "port": "p4", "kind": "bmc"}})
    # Pre-seed an operator bmc-reset request ahead of the auto enqueue so both
    # are claimed in one cycle. The bmc-reset path resolves a lease IP; stub it
    # to a no-lease defer so it never touches record_flap regardless.
    import flax_reconcile.cycle as cyc
    monkeypatch.setattr(cyc.db, "lease_ip_for_mac", lambda pool, mac: None)
    _patch_cycle(monkeypatch, stub)
    stub.seed_claim({"id": 99, "mac": op_mac, "switch": "s", "port": "p5",
                     "kind": "bmc", "reason": "operator bmc-reset",
                     "attempts": 0, "status": "claimed"})
    _reconciler(("switch_flap", True)).run_one_cycle(pool=object())
    # record_flap fired for the auto mac only.
    assert stub.record_flap_calls == [auto_mac]
    assert op_mac not in stub.record_flap_calls


# (d) flap_state is GC'd by AGE (stale-GC), not by mismatch-membership. The old
# membership-clear wiped a per-port flap-pong mac's cooldown the instant it
# momentarily converged; the cycle now ages rows out by flap_circuit_backoff_secs
# and no longer calls the membership-based clear_flap_state.
def test_stale_flap_state_gc_by_age(monkeypatch):
    now = _ts()
    still = "aa:bb:cc:00:00:06"
    converged = "aa:bb:cc:00:00:07"
    stub = _Stub(
        mismatches=[{"mac": still, "lease_ip": None, "reserved_ip": "1.1.1.6"}],
        flap_state={
            still: {"last_flap_at": now, "backoff_until": None, "faulted": False},
            converged: {"last_flap_at": now, "backoff_until": None,
                        "faulted": False}},
        now=now,
        locations={still: {"switch": "s", "port": "p6", "kind": "bmc"}})
    _patch_cycle(monkeypatch, stub)
    _reconciler(("switch_flap", True)).run_one_cycle(pool=object())
    # time-based GC uses the backoff window (DEFAULTS flap_circuit_backoff_secs=900);
    # the membership-based clear_flap_state is no longer used by the cycle.
    assert stub.clear_stale_calls == [900]
    assert stub.clear_flap_calls == []


# (e) run_one_cycle reclaims stale 'claimed' rows at the START of PASS 2, using
#     reclaim_stale_claim_secs from cfg, and surfaces the count as "reclaimed".
def test_cycle_reclaims_stale_claims_at_pass2_start(monkeypatch):
    now = _ts()
    stub = _Stub(mismatches=[], flap_state={}, now=now, locations={})
    stub.reclaimed = 13
    _patch_cycle(monkeypatch, stub)
    summary = _reconciler(("switch_flap", True)).run_one_cycle(pool=object())
    # called once, with the DEFAULTS threshold (reconciler omits the key so it
    # resolves from the DEFAULTS overlay).
    assert stub.reclaim_calls == [180]
    # the reclaimed count flows into the summary without dropping other keys.
    assert summary["reclaimed"] == 13
    assert "kicked" in summary and "mismatches" in summary


# Operator (non-auto) flap success also bypasses record_flap.
def test_operator_flap_success_does_not_record_flap(monkeypatch):
    now = _ts()
    op_mac = "aa:bb:cc:00:00:08"
    stub = _Stub(mismatches=[], flap_state={}, now=now, locations={})
    _patch_cycle(monkeypatch, stub)
    stub.seed_claim({"id": 5, "mac": op_mac, "switch": "s", "port": "p8",
                     "kind": "bmc", "reason": "operator flap", "attempts": 0,
                     "status": "claimed"})
    _reconciler(("switch_flap", True)).run_one_cycle(pool=object())
    assert stub.record_flap_calls == []


# ---------------------------------------------------------------------------
# stale-lease release on kick: the stale Kea lease is dropped BEFORE the flap
# on any flap-path kick whose device is on the wrong (pool) lease -- both the
# auto lease!=reservation kick AND an operator flap (so the re-DHCP lands on the
# reservation) -- but NOT for an operator bmc-reset (a Redfish reset, not a flap).
# ---------------------------------------------------------------------------
class _OrderingLadder:
    """Ladder that logs ("flap", mac) into a shared events list so a test can
    assert the stale-lease release ran BEFORE the flap."""

    def __init__(self, events, result):
        self.events = events
        self.result = result
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        self.events.append(("flap", kw["mac"]))
        return self.result


def _reconciler_with_ladder(ladder):
    return Reconciler(switches={}, config={"flap_hold_seconds": 2},
                      obmc_user="u", obmc_pass="p", host_creds=[],
                      ladder=ladder)


def test_auto_kick_releases_stale_lease_before_flap(monkeypatch):
    now = _ts()
    mac = "aa:bb:cc:00:00:09"
    stub = _Stub(
        mismatches=[{"mac": mac, "lease_ip": "10.0.0.5", "reserved_ip": "1.1.1.9"}],
        flap_state={},
        now=now,
        locations={mac: {"switch": "s", "port": "p9", "kind": "bmc"}})
    stub.release_result = ["10.0.0.5"]
    _patch_cycle(monkeypatch, stub)
    ladder = _OrderingLadder(stub.events, ("switch_flap", True))
    _reconciler_with_ladder(ladder).run_one_cycle(pool=object())
    # release happened, for this mac, and STRICTLY before the flap.
    assert stub.release_calls == [mac]
    assert stub.events == [("release", mac), ("flap", mac)]
    # the lease_release action was logged with the released IPs.
    rel = [a for a in stub.log_actions if a.get("action") == "lease_release"]
    assert len(rel) == 1
    assert rel[0]["detail"]["mac"] == mac
    assert rel[0]["detail"]["released"] == ["10.0.0.5"]
    assert rel[0]["outcome"] == "success"


def test_auto_kick_no_release_action_when_nothing_released(monkeypatch):
    now = _ts()
    mac = "aa:bb:cc:00:00:0a"
    stub = _Stub(
        mismatches=[{"mac": mac, "lease_ip": "1.1.1.10", "reserved_ip": "1.1.1.10"}],
        flap_state={},
        now=now,
        locations={mac: {"switch": "s", "port": "pa", "kind": "bmc"}})
    stub.release_result = []  # self-guard: nothing stale to release
    _patch_cycle(monkeypatch, stub)
    ladder = _OrderingLadder(stub.events, ("switch_flap", True))
    _reconciler_with_ladder(ladder).run_one_cycle(pool=object())
    # release attempted, but no lease_release action because nothing released.
    assert stub.release_calls == [mac]
    assert [a for a in stub.log_actions if a.get("action") == "lease_release"] == []


def test_operator_bmc_reset_does_not_release_stale_lease(monkeypatch):
    now = _ts()
    op_mac = "aa:bb:cc:00:00:0b"
    stub = _Stub(mismatches=[], flap_state={}, now=now, locations={})
    import flax_reconcile.cycle as cyc
    monkeypatch.setattr(cyc.db, "lease_ip_for_mac", lambda pool, mac: None)
    _patch_cycle(monkeypatch, stub)
    stub.seed_claim({"id": 7, "mac": op_mac, "switch": "s", "port": "pb",
                     "kind": "bmc", "reason": "operator bmc-reset",
                     "attempts": 0, "status": "claimed"})
    _reconciler(("switch_flap", True)).run_one_cycle(pool=object())
    assert stub.release_calls == []


def test_operator_flap_releases_stale_lease_before_flap(monkeypatch):
    """An operator flap of a device on a stale pool lease releases that lease
    BEFORE the flap, exactly like the auto mismatch kick -- otherwise the flap
    (or eth0 bounce) only makes the client RENEW the stale lease and it never
    converges onto its reservation. Regression guard for the bug where the
    release was gated on reason=='lease!=reservation' and operator flaps skipped
    it."""
    now = _ts()
    op_mac = "aa:bb:cc:00:00:0c"
    stub = _Stub(mismatches=[], flap_state={}, now=now,
                 locations={op_mac: {"switch": "s", "port": "pc", "kind": "bmc"}})
    stub.release_result = ["10.0.0.7"]
    _patch_cycle(monkeypatch, stub)
    ladder = _OrderingLadder(stub.events, ("switch_flap", True))
    stub.seed_claim({"id": 8, "mac": op_mac, "switch": "s", "port": "pc",
                     "kind": "bmc", "reason": "operator flap", "attempts": 0,
                     "status": "claimed"})
    _reconciler_with_ladder(ladder).run_one_cycle(pool=object())
    # released this mac, STRICTLY before the flap, and logged the action.
    assert stub.release_calls == [op_mac]
    assert stub.events == [("release", op_mac), ("flap", op_mac)]
    rel = [a for a in stub.log_actions if a.get("action") == "lease_release"]
    assert len(rel) == 1 and rel[0]["detail"]["mac"] == op_mac


# ---------------------------------------------------------------------------
# db.release_stale_lease: DB-free assertions on the SQL it issues + mac
# encoding. A fake pool/conn/cursor captures the executed statements without a
# live Postgres (the live behaviour is covered by the skip-gated integration
# tests when a DB is present).
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []  # [(sql, params)]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        # only the SECURITY DEFINER fn call returns rows
        if "kea.flax_release_stale_lease" in sql:
            return _FakeCursor(self._rows)
        return _FakeCursor([])


class _FakeConnCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


class _FakePool:
    def __init__(self, rows):
        self.conn = _FakeConn(rows)

    def connection(self):
        return _FakeConnCtx(self.conn)


def test_release_stale_lease_calls_security_definer_fn_and_encoding():
    """db.release_stale_lease now CALLs the SECURITY DEFINER fn
    kea.flax_release_stale_lease (migration 019) rather than issuing the raw
    DELETE -- flax_reconcile lacks DELETE on kea.lease4. Assert the fn call,
    the flattened RETURNING rows, and the mac bytea encoding."""
    pool = _FakePool([("10.0.0.5",), ("10.0.0.6",)])
    out = db.release_stale_lease(pool, "AA:BB:CC:00:00:09")
    # returns the fn-result rows flattened
    assert out == ["10.0.0.5", "10.0.0.6"]
    stmts = pool.conn.executed
    # exactly one statement: the fn call (no SET LOCAL, no raw DELETE)
    assert len(stmts) == 1
    sql, params = stmts[0]
    assert "SELECT kea.flax_release_stale_lease(%s)" == sql
    assert "DELETE FROM kea.lease4" not in sql
    assert "SET LOCAL" not in sql
    # mac hex-decoded to bytea (lowercased, colons stripped), positional param
    assert params == (bytes.fromhex("aabbcc000009"),)


def test_release_stale_lease_swallows_errors_returns_empty():
    """Best-effort: if the fn is absent (un-migrated DB) or any error fires,
    release_stale_lease logs a warning and returns [] so the reconcile cycle's
    kick still flaps instead of crashing."""
    class _BoomPool:
        def connection(self):
            raise RuntimeError("function kea.flax_release_stale_lease(bytea) "
                               "does not exist")

    out = db.release_stale_lease(_BoomPool(), "AA:BB:CC:00:00:09")
    assert out == []
