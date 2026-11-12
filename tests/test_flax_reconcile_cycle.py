"""Reconcile cycle orchestration, tested against a real Postgres.

Follows the repo skip convention (see test_flax_reconcile_db.py): open a psycopg
connection from FLAX_TEST_DSN or the libpq PG* env vars; pytest.skip if no DB is
reachable. Requires migrations 001-015 applied (kea schema + devices +
switch_facts + desired_port + reconcile_requests/_actions + intentional_flap +
audit.events). No local Postgres -> these tests SKIP (expected).

The B10 plan used an illustrative `flax_db` fixture; here it is translated to a
small wrapper (conn + pool) over a real connection, mirroring the DSN/cleanup
pattern. Cleanup is scoped to the test switches/MACs only.
"""
import json
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from flax_reconcile.cycle import Reconciler  # noqa: E402


# ---------------------------------------------------------------------------
# Module-scope fakes (these supersede any kick-only fakes; one copy).
# ---------------------------------------------------------------------------
class FakeLadder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return self.result


class FakeResetFn:
    """Records (bmc_ip, creds) calls; returns a scripted (ok, detail)."""
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, bmc_ip, creds):
        self.calls.append((bmc_ip, creds))
        return self.result


class RecordingSwitch:
    def __init__(self):
        self.vlan_set = None
        self.flapped = None

    def set_access_vlan(self, port, vid):
        self.vlan_set = (port, vid)

    def flap(self, port, hold_seconds=None):
        self.flapped = (port, hold_seconds)


# ---------------------------------------------------------------------------
# Pure unit: which flap reasons get a stale-lease release before the flap.
# (No DB — runs everywhere.)
# ---------------------------------------------------------------------------
def test_operator_flap_releases_stale_lease_before_flap():
    """An operator flap of a device on the wrong (pool) lease must drop that
    lease first, exactly like the auto mismatch path -- otherwise the flap only
    makes the client renew the stale lease and never converges."""
    from flax_reconcile.cycle import _release_before_flap
    assert _release_before_flap("operator flap") is True


def test_auto_mismatch_still_releases_stale_lease():
    from flax_reconcile.cycle import _release_before_flap
    assert _release_before_flap("lease!=reservation") is True


def test_bmc_reset_does_not_release_lease():
    """Redfish bmc-reset is not a flap and must not touch the lease."""
    from flax_reconcile.cycle import _release_before_flap
    assert _release_before_flap("operator bmc-reset") is False


# ---------------------------------------------------------------------------
# DSN + scoped cleanup
# ---------------------------------------------------------------------------
# NEVER use real switch names here: the fixture's cleanup DELETEs rows
# WHERE switch = ANY(SWITCHES) across reconcile_actions / intentional_flap /
# switch_facts / desired_port / audit.events. Run against a live DB with
# sufficient privileges, a real name wipes production rows -- this happened
# on bang-gouda 2026-07-05 (the old tuple carried a REAL production switch name; a superuser
# test run erased ~9 days of that real switch's reconcile_actions/audit history).
# tests/test_no_real_switch_names_in_db_fixtures.py pins the denylist.
SWITCHES = ("rabbit-cycletest-a", "rabbit-cycletest-b", "cycletest-s")
PORTS = ("Ethernet6/1", "Ethernet32/1", "p")
MACS = (
    "aa:bb:cc:00:00:01",
    "aa:bb:cc:00:00:02",
    "aa:bb:cc:00:00:03",
)


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


def _mac_hex(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").replace(".", "").lower()


def _cleanup(conn) -> None:
    hexes = [_mac_hex(m) for m in MACS]
    conn.execute(
        "DELETE FROM kea.lease4 WHERE hwaddr = ANY(%s::bytea[])",
        ([f"\\x{h}" for h in hexes],))
    conn.execute(
        "DELETE FROM kea.hosts "
        "WHERE dhcp_identifier_type = 0 AND dhcp_identifier = ANY(%s::bytea[])",
        ([f"\\x{h}" for h in hexes],))
    conn.execute("DELETE FROM devices WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_requests WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_flap_state WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_actions WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM intentional_flap WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM switch_facts WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM desired_port WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute(
        "DELETE FROM audit.events WHERE service = 'flax-reconcile' AND switch = ANY(%s)",
        (list(SWITCHES),))


class _FlaxDB:
    """Tiny wrapper so the test bodies read like the plan's flax_db fixture."""

    def __init__(self, conn, pool):
        self.conn = conn
        self.pool = pool


@pytest.fixture()
def flax_db():
    from flax_reconcile import db
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        pool = db.build_pool(_dsn())
    except Exception as exc:  # pragma: no cover - depends on env
        c.close()
        pytest.skip(f"no DB available: {exc}")
    try:
        _cleanup(c)
        yield _FlaxDB(c, pool)
    finally:
        try:
            _cleanup(c)
        except Exception:
            pass
        try:
            pool.close()
        except Exception:
            pass
        c.close()


# ---------------------------------------------------------------------------
# PASS 2: lease!=reservation kick
# ---------------------------------------------------------------------------
def test_cycle_enqueues_mismatch_then_kicks_and_completes(flax_db):
    # seed: reserved 172.17.0.5, leased elsewhere; device row gives location
    c = flax_db.conn
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000001','hex'), 0, 2886729733)")
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000001','hex'), 0)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb, NOW(), 1)")

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["kicked"] == 1
    assert ladder.calls[0]["switch"] == "rabbit-cycletest-a"
    row = c.execute("SELECT status, outcome FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:00:00:01'").fetchone()
    assert row == ("done", "switch_flap")
    act = c.execute("SELECT outcome FROM reconcile_actions ORDER BY id DESC LIMIT 1").fetchone()
    assert act[0] == "success"


def test_cycle_kick_passes_port_access_vid_to_ladder(flax_db):
    """The kick must hand run_ladder the port's access VID (not None), so the
    bmc_ll rung (kind=='bmc' and vid) can SSH the BMC on the right vlan sub-iface
    instead of always falling through to switch_flap. Regression: cycle.py
    hardcoded vid=None, disabling bmc_ll/host_ssh."""
    c = flax_db.conn
    # BMC reserved .5, leased .99 (wrong) -> mismatch; port is on access vlan 17.
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000001','hex'), 0, 2886729733)")
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000001','hex'), 0)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb, NOW(), 1)")
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, 'rabbit-cycletest-a', "
        "'{\"Ethernet6/1\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"link\"}}'::jsonb, "
        "true, 1, NOW())")

    ladder = FakeLadder(("bmc_ll", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["kicked"] == 1
    assert ladder.calls[0]["kind"] == "bmc"
    assert ladder.calls[0]["vid"] == 17


def test_cycle_defers_on_ladder_failure(flax_db):
    c = flax_db.conn
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000002','hex'), 0, 2886729733)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:02','cycletest-s','p','bmc','{}'::jsonb, NOW(), 1)")
    # Reserved but no lease: the cycle only kicks a no-lease BMC whose port
    # shows link-up (an absent/powered-off device is never kicked), so the
    # port must be link-up for the kick -- and the ladder failure -- to happen.
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, 'cycletest-s', "
        "'{\"p\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"link\"}}'::jsonb, "
        "true, 1, NOW())")
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[],
                   ladder=FakeLadder((None, False)))
    r.run_one_cycle(flax_db.pool)
    row = c.execute("SELECT status, attempts FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:00:00:02'").fetchone()
    assert row == ("pending", 1)  # deferred for retry


# ---------------------------------------------------------------------------
# Operator BMC-reset dispatch (reason='operator bmc-reset')
# ---------------------------------------------------------------------------
def test_cycle_dispatches_bmc_reset_reason_to_reset_path(flax_db):
    """An operator 'operator bmc-reset' request resolves the BMC IP from the
    mac's active lease and fires the reset fn, NOT the kick ladder. Verifies the
    branch via an injected fake reset fn + fake ladder."""
    c = flax_db.conn
    # Active lease 172.17.0.99 (= 2886795363) for the bmc mac (resolves the BMC IP).
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886795363, decode('aabbcc000001','hex'), 0)")
    # Operator-enqueued reset request for this mac.
    c.execute("INSERT INTO reconcile_requests "
              "(requested_by, mac, switch, port, kind, reason) "
              "VALUES ('op:ui','aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1',"
              "'bmc','operator bmc-reset')")

    ladder = FakeLadder(("switch_flap", True))  # must NOT be called
    reset = FakeResetFn((True, "Manager.Reset accepted (HTTP 204)"))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder,
                   redfish_creds=[{"bmcuser": "a", "bmcpass": "b"}],
                   bmc_reset_fn=reset)
    r.run_one_cycle(flax_db.pool)

    # reset fired against the leased IP; ladder never touched.
    assert reset.calls == [("172.17.0.99", [{"bmcuser": "a", "bmcpass": "b"}])]
    assert ladder.calls == []
    # request completed with bmc_reset outcome
    row = c.execute("SELECT status, outcome FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:00:00:01'").fetchone()
    assert row == ("done", "bmc_reset")
    act = c.execute("SELECT action, outcome FROM reconcile_actions "
                    "ORDER BY id DESC LIMIT 1").fetchone()
    assert act == ("bmc_reset", "success")


def test_cycle_defers_bmc_reset_when_reset_fails(flax_db):
    """A failing reset fn defers the request (pending, attempts=1)."""
    c = flax_db.conn
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000002','hex'), 0)")
    c.execute("INSERT INTO reconcile_requests "
              "(requested_by, mac, switch, port, kind, reason) "
              "VALUES ('op:ui','aa:bb:cc:00:00:02','cycletest-s','p','bmc',"
              "'operator bmc-reset')")
    reset = FakeResetFn((False, "GET /Managers HTTP 401"))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[],
                   ladder=FakeLadder((None, False)),
                   redfish_creds=[{"bmcuser": "a", "bmcpass": "b"}],
                   bmc_reset_fn=reset)
    r.run_one_cycle(flax_db.pool)
    row = c.execute("SELECT status, attempts FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:00:00:02'").fetchone()
    assert row == ("pending", 1)


def test_cycle_bmc_reset_no_lease_defers_without_calling_reset(flax_db):
    """No active lease for the bmc mac -> defer, reset fn never called."""
    c = flax_db.conn
    c.execute("INSERT INTO reconcile_requests "
              "(requested_by, mac, switch, port, kind, reason) "
              "VALUES ('op:ui','aa:bb:cc:00:00:03','rabbit-cycletest-a','Ethernet6/1',"
              "'bmc','operator bmc-reset')")
    reset = FakeResetFn((True, "should not be called"))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[],
                   ladder=FakeLadder((None, False)),
                   redfish_creds=[{"bmcuser": "a", "bmcpass": "b"}],
                   bmc_reset_fn=reset)
    r.run_one_cycle(flax_db.pool)
    assert reset.calls == []  # no lease -> reset never attempted
    row = c.execute("SELECT status, attempts FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:00:00:03'").fetchone()
    assert row == ("pending", 1)


# ---------------------------------------------------------------------------
# PASS 1: VLAN steer
# ---------------------------------------------------------------------------
def test_cycle_steers_access_port_and_skips_kick(flax_db):
    c = flax_db.conn
    # classify wants this access port (currently vid 17) moved to family vid 16
    c.execute("INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
              "VALUES ('eos', '{}'::jsonb, 'rabbit-cycletest-a', '{\"Ethernet6/1\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"link\"}}'::jsonb, true, 1, NOW())")
    c.execute("INSERT INTO desired_port (switch, port, desired_vid, occupants, generation) "
              "VALUES ('rabbit-cycletest-a','Ethernet6/1',16,'{}'::jsonb,1)")
    # Seed a kea reservation + a DIFFERING lease for the same MAC, so there IS
    # a lease!=reservation mismatch for this port.  The steer must suppress the
    # kick (kicked==0) without the ladder being invoked for it.
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000001','hex'), 0, 2886729733)")   # reserved 172.17.0.5
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000001','hex'), 0)")    # leased 172.17.0.99 (different)
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb,NOW(),1)")
    sw = RecordingSwitch()
    ladder = FakeLadder((None, False))
    r = Reconciler(switches={"rabbit-cycletest-a": sw},
                   config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                           "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="", obmc_pass="", host_creds=[], no_steer=set(),
                   ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["steered"] == 1
    assert sw.vlan_set == ("Ethernet6/1", 16)
    assert sw.flapped == ("Ethernet6/1", 2)
    # The steer succeeded, so the kick must be suppressed
    assert summary["kicked"] == 0
    assert ladder.calls == []  # ladder never called for the steered port
    # sentinel written before the flap
    assert c.execute("SELECT count(*) FROM intentional_flap "
                     "WHERE switch='rabbit-cycletest-a' AND port='Ethernet6/1'").fetchone()[0] == 1


def test_cycle_refuses_steer_on_no_steer_listed_port(flax_db):
    c = flax_db.conn
    c.execute("INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
              "VALUES ('eos', '{}'::jsonb, 'rabbit-cycletest-b', '{\"Ethernet32/1\": {\"mask\":\"access\",\"access_vid\":31,\"link\":\"link\"}}'::jsonb, true, 1, NOW())")
    c.execute("INSERT INTO desired_port (switch, port, desired_vid, occupants, generation) "
              "VALUES ('rabbit-cycletest-b','Ethernet32/1',16,'{}'::jsonb,1)")
    sw = RecordingSwitch()
    r = Reconciler(switches={"rabbit-cycletest-b": sw},
                   config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                           "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="", obmc_pass="", host_creds=[],
                   no_steer={("rabbit-cycletest-b", "Ethernet32/1")},
                   ladder=FakeLadder((None, False)))
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["refused"] == 1
    assert sw.vlan_set is None and sw.flapped is None  # never touched the uplink
    act = c.execute("SELECT outcome, reason FROM reconcile_actions "
                    "WHERE switch='rabbit-cycletest-b' ORDER BY id DESC LIMIT 1").fetchone()
    assert act == ("deferred", "no_steer_listed")
    # _emit_fault must have written an audit.events row for operator visibility
    fault_count = c.execute(
        "SELECT count(*) FROM audit.events "
        "WHERE service='flax-reconcile' AND kind='steer_refused' AND switch='rabbit-cycletest-b'"
    ).fetchone()[0]
    assert fault_count == 1


# ---------------------------------------------------------------------------
# PASS 2: link-gate — no-lease reservations only kicked when port is link-up
# ---------------------------------------------------------------------------

def test_cycle_skips_kick_for_no_lease_link_down(flax_db):
    """No active lease + port link-down (device absent) -> not kicked."""
    c = flax_db.conn
    # Reservation exists, no lease row at all (powered-off device)
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000001','hex'), 0, 2886729733)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb,NOW(),1)")
    # switch_facts with link=nolink (port down)
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, 'rabbit-cycletest-a', "
        "'{\"Ethernet6/1\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"nolink\"}}'::jsonb, "
        "true, 1, NOW())")

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    # Must not enqueue or kick — device is absent
    assert summary["enqueued"] == 0
    assert summary["kicked"] == 0
    assert ladder.calls == []
    row = c.execute("SELECT count(*) FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:00:00:01'").fetchone()[0]
    assert row == 0


def test_cycle_kicks_no_lease_when_port_link_up(flax_db):
    """No active lease + port link-up (device present, hasn't DHCPed) -> kicked."""
    c = flax_db.conn
    # Reservation exists, no lease row (device powered on but hasn't DHCPed yet)
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000002','hex'), 0, 2886729733)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:02','cycletest-s','p','bmc','{}'::jsonb,NOW(),1)")
    # switch_facts with link=link (port up)
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, 'cycletest-s', "
        "'{\"p\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"link\"}}'::jsonb, "
        "true, 1, NOW())")

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    # Device is present (link up) even without a lease — must kick
    assert summary["enqueued"] == 1
    assert summary["kicked"] == 1
    assert ladder.calls[0]["switch"] == "cycletest-s"


# ---------------------------------------------------------------------------
# PASS 2: install-gate — a mid-install host's port is not kicked
# ---------------------------------------------------------------------------

def test_cycle_skips_kick_for_no_lease_port_mid_install(flax_db, monkeypatch):
    """No active lease + port link-UP but host is mid-PXE-install -> NOT kicked.

    The install gate must win over the link-up kick: flapping interrupts the
    install. We inject db.read_installing_ports to return this port's
    (switch, port) so the test is DB-state-independent for the install set."""
    from flax_reconcile import db
    c = flax_db.conn
    # Reservation exists, no lease (device present, PXE-installing, no DHCP yet).
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000001','hex'), 0, 2886729733)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb,NOW(),1)")
    # Port link UP (would normally kick) — proves the install gate wins.
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, 'rabbit-cycletest-a', "
        "'{\"Ethernet6/1\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"link\"}}'::jsonb, "
        "true, 1, NOW())")
    # resolve_location canonicalizes devices.port to Arista (Ethernet6/1), so the
    # installing set must use that same form to match loc_key.
    monkeypatch.setattr(db, "read_installing_ports",
                        lambda pool: {("rabbit-cycletest-a", "Ethernet6/1")})

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    # Mid-install: must NOT enqueue or kick even though the port is link-up.
    assert summary["enqueued"] == 0
    assert summary["kicked"] == 0
    assert ladder.calls == []
    cnt = c.execute("SELECT count(*) FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:00:00:01'").fetchone()[0]
    assert cnt == 0


def test_cycle_kicks_no_lease_link_up_when_not_installing(flax_db, monkeypatch):
    """Existing behaviour preserved: no-lease + link-up + NOT installing -> kicked
    (install set empty)."""
    from flax_reconcile import db
    c = flax_db.conn
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000002','hex'), 0, 2886729733)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:02','cycletest-s','p','bmc','{}'::jsonb,NOW(),1)")
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, 'cycletest-s', "
        "'{\"p\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"link\"}}'::jsonb, "
        "true, 1, NOW())")
    monkeypatch.setattr(db, "read_installing_ports", lambda pool: set())

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["enqueued"] == 1
    assert summary["kicked"] == 1
    assert ladder.calls[0]["switch"] == "cycletest-s"


# ---------------------------------------------------------------------------
# PASS 2: BMC-only flap rule — only a BMC mismatch may flap the port.
# Once the BMC holds its reservation lease the port is never flapped again, so a
# finished/powered-off host (whose NC-SI sideband keeps the port link UP) no
# longer drives a futile, SOL-dropping flap. A host (or vm) mismatch converges
# at its next DHCP (PXE boot / power cycle), not via a port bounce.
# ---------------------------------------------------------------------------

def test_cycle_does_not_kick_host_mismatch(flax_db):
    """A HOST-kind mismatch (wrong-IP lease) must NOT flap the port. Even with a
    link-up port and a clear lease!=reservation, a host kind is left alone."""
    c = flax_db.conn
    # Reserved 172.17.0.5, leased 172.17.0.99 (wrong IP) — but kind=host.
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000003','hex'), 0, 2886729733)")
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000003','hex'), 0)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:03','rabbit-cycletest-a','Ethernet6/1','host','{}'::jsonb,NOW(),1)")
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, 'rabbit-cycletest-a', "
        "'{\"Ethernet6/1\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"link\"}}'::jsonb, "
        "true, 1, NOW())")

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["enqueued"] == 0
    assert summary["kicked"] == 0
    assert ladder.calls == []
    cnt = c.execute("SELECT count(*) FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:00:00:03'").fetchone()[0]
    assert cnt == 0


def test_cycle_still_kicks_bmc_mismatch(flax_db):
    """A BMC-kind mismatch still flaps (the only thing that may): the flap is how
    the BMC lands on its reservation. Mirrors the wrong-IP kick but asserts the
    kind gate lets a bmc through."""
    c = flax_db.conn
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000001','hex'), 0, 2886729733)")
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000001','hex'), 0)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb,NOW(),1)")

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["enqueued"] == 1
    assert summary["kicked"] == 1
    assert ladder.calls[0]["switch"] == "rabbit-cycletest-a"


def test_cycle_install_gate_does_not_affect_wrong_ip_kick(flax_db, monkeypatch):
    """A device WITH a (wrong) lease is kicked regardless of install-state: the
    install gate only guards the NO-LEASE branch. Even with the port in the
    installing set, the lease!=reservation kick still fires."""
    from flax_reconcile import db
    c = flax_db.conn
    # Reserved 172.17.0.5, leased 172.17.0.99 (wrong IP).
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000003','hex'), 0, 2886729733)")
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000003','hex'), 0)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:03','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb,NOW(),1)")
    # Port IS in the installing set — must not block the has-lease kick.
    monkeypatch.setattr(db, "read_installing_ports",
                        lambda pool: {("rabbit-cycletest-a", "Ethernet6/1")})

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["enqueued"] == 1
    assert summary["kicked"] == 1
    assert ladder.calls[0]["switch"] == "rabbit-cycletest-a"


def test_cycle_always_kicks_wrong_ip_regardless_of_link(flax_db):
    """Device leased on wrong IP (has lease != reserved_ip) -> always kicked."""
    c = flax_db.conn
    # Reserved 172.17.0.5, leased 172.17.0.99 (wrong IP — device is present)
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000003','hex'), 0, 2886729733)")
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000003','hex'), 0)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:03','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb,NOW(),1)")
    # switch_facts with link=nolink (shouldn't matter when lease is present)
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, 'rabbit-cycletest-a', "
        "'{\"Ethernet6/1\": {\"mask\":\"access\",\"access_vid\":17,\"link\":\"nolink\"}}'::jsonb, "
        "true, 1, NOW())")

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    # Wrong IP: kick regardless of link state
    assert summary["enqueued"] == 1
    assert summary["kicked"] == 1
    assert ladder.calls[0]["switch"] == "rabbit-cycletest-a"


# ---------------------------------------------------------------------------
# recent_unfinished_flap_ports (self-heal fallback gate, 2026-11-06)
# ---------------------------------------------------------------------------
def _act(c, port, action, outcome, ago_s):
    c.execute("INSERT INTO reconcile_actions (ts, switch, port, action, detail, outcome) "
              "VALUES (NOW() - make_interval(secs => %s), %s, %s, %s, '{}'::jsonb, %s)",
              (ago_s, SWITCHES[0], port, action, outcome))


def _recovered(c, port, ago_s):
    c.execute("INSERT INTO audit.events (ts, service, kind, switch, port, payload) "
              "VALUES (NOW() - make_interval(secs => %s), 'flax-reconcile', 'port_recovered', %s, %s, '{}'::jsonb)",
              (ago_s, SWITCHES[0], port))


def _recent(flax_db):
    from flax_reconcile import db
    return {k for k in db.recent_unfinished_flap_ports(flax_db.pool) if k[0] in SWITCHES}


def test_recent_unfinished_flap_ports_failure_without_later_success(flax_db):
    _act(flax_db.conn, PORTS[0], "switch_flap", "failure", 60)
    assert _recent(flax_db) == {(SWITCHES[0], PORTS[0])}


def test_recent_unfinished_flap_ports_later_success_clears(flax_db):
    _act(flax_db.conn, PORTS[0], "switch_flap", "failure", 120)
    _act(flax_db.conn, PORTS[0], "switch_flap", "success", 60)
    assert _recent(flax_db) == set()


def test_recent_unfinished_flap_ports_later_port_recovered_clears(flax_db):
    _act(flax_db.conn, PORTS[0], "switch_flap", "failure", 120)
    _recovered(flax_db.conn, PORTS[0], 60)
    assert _recent(flax_db) == set()


def test_recent_unfinished_flap_ports_window(flax_db):
    _act(flax_db.conn, PORTS[0], "switch_flap", "failure", 7200)
    assert _recent(flax_db) == set()


def test_recent_unfinished_flap_ports_ignores_deferred_refusals(flax_db):
    _act(flax_db.conn, PORTS[0], "set_vlan", "deferred", 60)
    assert _recent(flax_db) == set()


def test_read_switch_facts_ports_carries_admin(flax_db):
    from flax_reconcile import db
    import json
    flax_db.conn.execute(
        "INSERT INTO switch_facts (switch, driver, polled_at, reachable, generation, port_mask, ports) "
        "VALUES (%s, 'eos', NOW(), true, 1, '{}'::jsonb, %s::jsonb)",
        (SWITCHES[0], json.dumps({PORTS[0]: {"link": "nolink", "mask": "access", "access_vid": 25, "admin": "down"},
                                  PORTS[1]: {"link": "nolink", "mask": "access", "access_vid": 25}})))
    out = db.read_switch_facts_ports(flax_db.pool)
    assert out[(SWITCHES[0], PORTS[0])]["admin"] == "down"
    assert out[(SWITCHES[0], PORTS[1])]["admin"] is None


# ---------------------------------------------------------------------------
# post BMC on a pool lease (reconcile_pool_bmc, 2026-11-06)
# ---------------------------------------------------------------------------
_V24 = 2887254016           # 172.24.0.0
_POOL_IP = _V24 + 60        # 172.24.0.60
_RES_IP = _V24 + 7 * 256 + 101   # 172.24.7.101
_OLD_V25 = _V24 + 65536 + 7 * 256 + 101  # 172.25.7.101


def _post_host_row(c, mac_hex, ip, kind="bmc"):
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address, user_context) "
              "VALUES (decode(%s,'hex'), 0, %s, %s)",
              (mac_hex, ip, json.dumps({"source": "post", "classify": {"kind": kind, "vid": 24}})))


def _lease(c, mac_hex, ip):
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) VALUES (%s, decode(%s,'hex'), 0)",
              (ip, mac_hex))


def _pool_rows(flax_db):
    from flax_reconcile import db
    return db.read_pool_bmc_reservations(flax_db.pool, frozenset({"post"}),
                                         pool_start=50, pool_end=200)


def test_pool_bmc_reservation_on_pool_lease_is_returned(flax_db):
    _post_host_row(flax_db.conn, "aabbcc000001", _RES_IP)
    _lease(flax_db.conn, "aabbcc000001", _POOL_IP)
    assert _pool_rows(flax_db) == [{"mac": "aa:bb:cc:00:00:01", "ip": "172.24.7.101"}]


def test_pool_bmc_reservation_on_its_reservation_is_not_returned(flax_db):
    _post_host_row(flax_db.conn, "aabbcc000001", _RES_IP)
    _lease(flax_db.conn, "aabbcc000001", _RES_IP)
    assert _pool_rows(flax_db) == []


def test_pool_bmc_host_row_is_never_returned(flax_db):
    _post_host_row(flax_db.conn, "aabbcc000002", _V24 + 7 * 256 + 1, kind="host")
    _lease(flax_db.conn, "aabbcc000002", _POOL_IP)
    assert _pool_rows(flax_db) == []


def test_pool_bmc_non_pool_stale_lease_is_not_returned(flax_db):
    _post_host_row(flax_db.conn, "aabbcc000001", _RES_IP)
    _lease(flax_db.conn, "aabbcc000001", _OLD_V25)
    assert _pool_rows(flax_db) == []


def _pool_kick_cycle(flax_db, sources):
    c = flax_db.conn
    _post_host_row(c, "aabbcc000001", _RES_IP)
    _lease(c, "aabbcc000001", _POOL_IP)
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb, NOW(), 1)")
    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder,
                   eligible_sources=frozenset({"triage", "legacy-import", ""}),
                   pool_bmc_sources=sources)
    summary = r.run_one_cycle(flax_db.pool)
    return summary, ladder


def test_cycle_kicks_post_bmc_off_pool_lease_when_opted_in(flax_db):
    summary, ladder = _pool_kick_cycle(flax_db, frozenset({"post"}))
    assert summary["kicked"] == 1
    assert ladder.calls[0]["switch"] == "rabbit-cycletest-a"


def test_cycle_ignores_post_bmc_pool_lease_when_not_opted_in(flax_db):
    summary, ladder = _pool_kick_cycle(flax_db, frozenset())
    assert summary["enqueued"] == 0 and ladder.calls == []
