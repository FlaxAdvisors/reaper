"""End-to-end integration test for flax_reconcile — both passes.

Requires a real migrated DB (migrations 001-015). Without one, all tests SKIP.

Convention mirrors test_flax_reconcile_cycle.py:
  - pytest.importorskip("psycopg")
  - DSN from FLAX_TEST_DSN or PG* env vars
  - scoped _cleanup: clears test rows for the three test switches + three MACs
  - no `flax_db` fixture from conftest — self-contained connection + pool

Scenarios covered:
  (a) same-VLAN lease!=reservation mismatch -> reconcile_requests pending->done,
      RecordingSwitch.flapped records the port (kicked via switch_flap rung).
  (b) desired_port steer on a mask=access port (vid differs) ->
      set_access_vlan called + intentional_flap sentinel written + reconcile_actions
      set_vlan/success; the steered port is NOT also kicked (steer subsumes kick).
  (c) desired_port steer on a no-steer-listed port -> set_access_vlan NOT called,
      reconcile_actions deferred/no_steer_listed, audit.events steer_refused fault.
  (d) read_active_sentinels returns the steered port AND the kicked port, proving
      flax-observe would freeze them.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from flax_reconcile.cycle import Reconciler  # noqa: E402
from flax_reconcile.kick import run_ladder   # noqa: E402
from flax_observe.persistence import read_active_sentinels  # noqa: E402


# ---------------------------------------------------------------------------
# Test-scoped identifiers — chosen to avoid collisions with other test suites.
# ---------------------------------------------------------------------------
_SW_KICK   = "rabbit-integ-kick"      # case (a): kick only switch
_SW_STEER  = "rabbit-integ-steer"     # case (b): steer (access port)
_SW_NOSTEER = "rabbit-integ-nosteer"  # case (c): steer refused (no-steer port)

_SWITCHES = (_SW_KICK, _SW_STEER, _SW_NOSTEER)

_PORT_KICK   = "Ethernet1/1"          # Arista canonical (flap/sentinel/reqs use this)
_PORT_KICK_INTERNAL = "et1b1"         # what flax-discover actually writes to devices.port
_PORT_STEER  = "Ethernet2/1"
_PORT_NOSTEER = "Ethernet3/1"

# Unique MACs — valid hex, not in any real data set.
_MAC_KICK   = "dd:ee:ff:00:01:01"   # (a): mismatch kick
_MAC_STEER  = "dd:ee:ff:00:01:02"   # (b): steer occupant
# (c) no-steer port: no occupant MAC needed; only a desired_port row.

_MACS = (_MAC_KICK, _MAC_STEER)

# IP arithmetic: 172*16777216 + VID*65536 + 0*256 + N
#   172.20.0.5  = 2886991877
#   172.20.0.99 = 2886991971
#   172.20.0.6  = 2886991878  (steer occupant reservation)
_IP_KICK_RESERVED  = 2886991877   # 172.20.0.5
_IP_KICK_LEASED    = 2886991971   # 172.20.0.99  (differs -> mismatch)
_IP_STEER_RESERVED = 2886991878   # 172.20.0.6   (any value; not leased)


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


def _mac_hex(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").replace(".", "").lower()


def _cleanup(conn) -> None:
    hexes = [_mac_hex(m) for m in _MACS]
    conn.execute(
        "DELETE FROM kea.lease4 WHERE hwaddr = ANY(%s::bytea[])",
        ([r"\x" + h for h in hexes],))
    conn.execute(
        "DELETE FROM kea.hosts "
        "WHERE dhcp_identifier_type = 0 AND dhcp_identifier = ANY(%s::bytea[])",
        ([r"\x" + h for h in hexes],))
    conn.execute("DELETE FROM devices WHERE mac = ANY(%s)", (list(_MACS),))
    conn.execute("DELETE FROM reconcile_requests WHERE mac = ANY(%s)", (list(_MACS),))
    conn.execute("DELETE FROM reconcile_flap_state WHERE mac = ANY(%s)", (list(_MACS),))
    conn.execute("DELETE FROM reconcile_actions WHERE switch = ANY(%s)", (list(_SWITCHES),))
    conn.execute("DELETE FROM intentional_flap WHERE switch = ANY(%s)", (list(_SWITCHES),))
    conn.execute("DELETE FROM switch_facts WHERE switch = ANY(%s)", (list(_SWITCHES),))
    conn.execute("DELETE FROM desired_port WHERE switch = ANY(%s)", (list(_SWITCHES),))
    conn.execute(
        "DELETE FROM audit.events WHERE service = 'flax-reconcile' AND switch = ANY(%s)",
        (list(_SWITCHES),))


class _FlaxDB:
    def __init__(self, conn, pool):
        self.conn = conn
        self.pool = pool


@pytest.fixture()
def flax_db():
    from flax_reconcile import db
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip("no DB available: " + str(exc))
    try:
        pool = db.build_pool(_dsn())
    except Exception as exc:  # pragma: no cover - depends on env
        c.close()
        pytest.skip("no DB available: " + str(exc))
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


class RecordingSwitch:
    """Records set_access_vlan and flap calls — does not touch a real switch."""
    def __init__(self):
        self.vlan_sets = []   # list of (port, vid)
        self.flapped = []     # list of (port, hold_seconds)

    def set_access_vlan(self, port, vid):
        self.vlan_sets.append((port, vid))

    def flap(self, port, hold_seconds=None):
        self.flapped.append((port, hold_seconds))


def test_integration_both_passes(flax_db):
    """Drive run_one_cycle against a real DB and assert (a)-(d).

    Seed:
      - _SW_KICK / _PORT_KICK / _MAC_KICK: kea reservation != lease (mismatch
        only; no desired_port, no switch_facts needed for kick).
      - _SW_STEER / _PORT_STEER / _MAC_STEER: switch_facts mask=access vid=20,
        desired_port vid=21; kea reservation set so compute_mismatches would
        flag it too — steer must suppress the kick.
      - _SW_NOSTEER / _PORT_NOSTEER: switch_facts mask=access vid=30,
        desired_port vid=31; port is in no_steer -> deferred/no_steer_listed.
    """
    c = flax_db.conn

    # ---- (a) kick: reservation at 172.20.0.5, lease at 172.20.0.99 ----------
    c.execute(
        "INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
        "VALUES (decode(%s,'hex'), 0, %s)",
        (_mac_hex(_MAC_KICK), _IP_KICK_RESERVED))
    c.execute(
        "INSERT INTO kea.lease4 (address, hwaddr, state) VALUES (%s, decode(%s,'hex'), 0)",
        (_IP_KICK_LEASED, _mac_hex(_MAC_KICK)))
    # devices.port is the REAL internal short form (et1b1) flax-discover writes;
    # resolve_location must canonicalize it to Arista (Ethernet1/1) so the flap +
    # sentinel + reconcile_requests are Arista-consistent. This is the seam the
    # canonicalization fix exercises.
    c.execute(
        "INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
        "VALUES (%s, %s, %s, 'bmc', '{}'::jsonb, NOW(), 1)",
        (_MAC_KICK, _SW_KICK, _PORT_KICK_INTERNAL))

    # ---- (b) steer: access port, vid differs; also has a mismatch (steer wins) -
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, %s, %s::jsonb, true, 1, NOW())",
        (_SW_STEER, '{"' + _PORT_STEER + '": {"mask":"access","access_vid":20,"link":"link"}}'))
    c.execute(
        "INSERT INTO desired_port (switch, port, desired_vid, occupants, generation) "
        "VALUES (%s, %s, 21, '{}'::jsonb, 1)",
        (_SW_STEER, _PORT_STEER))
    # Seed a reservation for the steer occupant + a DIFFERING lease so there
    # would normally be a kick — the steer must suppress it (kicked==0 for it).
    c.execute(
        "INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
        "VALUES (decode(%s,'hex'), 0, %s)",
        (_mac_hex(_MAC_STEER), _IP_STEER_RESERVED))
    c.execute(
        "INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
        "VALUES (%s, %s, %s, 'bmc', '{}'::jsonb, NOW(), 1)",
        (_MAC_STEER, _SW_STEER, _PORT_STEER))

    # ---- (c) no-steer: access port, vid differs, but listed in no_steer ------
    c.execute(
        "INSERT INTO switch_facts (driver, port_mask, switch, ports, reachable, generation, polled_at) "
        "VALUES ('eos', '{}'::jsonb, %s, %s::jsonb, true, 1, NOW())",
        (_SW_NOSTEER, '{"' + _PORT_NOSTEER + '": {"mask":"access","access_vid":30,"link":"link"}}'))
    c.execute(
        "INSERT INTO desired_port (switch, port, desired_vid, occupants, generation) "
        "VALUES (%s, %s, 31, '{}'::jsonb, 1)",
        (_SW_NOSTEER, _PORT_NOSTEER))

    # ---- Build Reconciler with RecordingSwitches and real run_ladder ----------
    sw_kick   = RecordingSwitch()
    sw_steer  = RecordingSwitch()
    sw_nosteer = RecordingSwitch()

    no_steer = {(_SW_NOSTEER, _PORT_NOSTEER)}

    r = Reconciler(
        switches={
            _SW_KICK:    sw_kick,
            _SW_STEER:   sw_steer,
            _SW_NOSTEER: sw_nosteer,
        },
        config={
            "flap_hold_seconds": 2,
            "kick_cooldown_secs": 60,
            "max_attempts": 3,
            "bmc_ll_probe_interval_secs": 60,
        },
        obmc_user="",   # bmc_ll rung no-ops (empty creds)
        obmc_pass="",   # host_ssh rung no-ops (no host_creds)
        host_creds=[],
        no_steer=no_steer,
        ladder=run_ladder,
    )

    summary = r.run_one_cycle(flax_db.pool)

    # ---- Assert (a): kick mismatch -> pending->done + port flapped -----------
    assert summary["kicked"] == 1, (
        "expected 1 kick (mismatch for _MAC_KICK), got " + str(summary["kicked"]))
    # devices.port was seeded as et1b1; the flap MUST use the Arista form
    # (Ethernet1/1) — this asserts resolve_location's canonicalization.
    assert (_PORT_KICK, 2) in sw_kick.flapped, (
        "expected sw_kick.flapped to contain the Arista-form kicked port "
        "(et1b1 must canonicalize to Ethernet1/1)")
    assert (_PORT_KICK_INTERNAL, 2) not in sw_kick.flapped, (
        "flap must NOT use the internal form et1b1 (invalid Arista CLI syntax)")
    kick_req = c.execute(
        "SELECT status, port FROM reconcile_requests WHERE mac = %s",
        (_MAC_KICK,)).fetchone()
    assert kick_req is not None, "reconcile_requests row must exist for kicked MAC"
    assert kick_req[0] == "done", (
        "reconcile_requests status must be 'done', got " + str(kick_req[0]))
    assert kick_req[1] == _PORT_KICK, (
        "reconcile_requests.port must be the Arista form Ethernet1/1 "
        "(resolve_location canonicalizes devices.port et1b1), got "
        + str(kick_req[1]))

    # ---- Assert (b): steer -> set_access_vlan called + sentinel + action row -
    assert summary["steered"] == 1, (
        "expected 1 steer (access port with desired_vid!=current), got " + str(summary["steered"]))
    assert (_PORT_STEER, 21) in sw_steer.vlan_sets, (
        "set_access_vlan must be called with the desired vid")
    assert (_PORT_STEER, 2) in sw_steer.flapped, (
        "port must be flapped after set_access_vlan (sentinel-first)")
    sentinel_row = c.execute(
        "SELECT hold_seconds FROM intentional_flap "
        "WHERE switch = %s AND port = %s",
        (_SW_STEER, _PORT_STEER)).fetchone()
    assert sentinel_row is not None, "intentional_flap sentinel must be written for steered port"
    steer_action = c.execute(
        "SELECT action, outcome FROM reconcile_actions "
        "WHERE switch = %s AND port = %s ORDER BY id DESC LIMIT 1",
        (_SW_STEER, _PORT_STEER)).fetchone()
    assert steer_action == ("set_vlan", "success"), (
        "reconcile_actions must have set_vlan/success for steered port, got " + str(steer_action))
    # The steer occupant (_MAC_STEER) also had a mismatch; steer must suppress kick.
    steer_mac_req = c.execute(
        "SELECT status FROM reconcile_requests WHERE mac = %s",
        (_MAC_STEER,)).fetchone()
    assert steer_mac_req is None, (
        "steered port must not also be kicked — steer subsumes kick")

    # ---- Assert (c): no-steer -> set_access_vlan NOT called + deferred action + audit fault -
    assert summary["refused"] == 1, (
        "expected 1 refusal (no_steer_listed port), got " + str(summary["refused"]))
    assert sw_nosteer.vlan_sets == [], (
        "set_access_vlan must NOT be called for no-steer-listed port")
    assert sw_nosteer.flapped == [], (
        "no-steer port must NOT be flapped")
    nosteer_action = c.execute(
        "SELECT action, outcome, reason FROM reconcile_actions "
        "WHERE switch = %s AND port = %s ORDER BY id DESC LIMIT 1",
        (_SW_NOSTEER, _PORT_NOSTEER)).fetchone()
    assert nosteer_action is not None, "reconcile_actions row must exist for refused steer"
    assert nosteer_action[1] == "deferred" and nosteer_action[2] == "no_steer_listed", (
        "reconcile_actions outcome/reason must be deferred/no_steer_listed, got "
        + str(nosteer_action))
    audit_fault = c.execute(
        "SELECT count(*) FROM audit.events "
        "WHERE service = 'flax-reconcile' AND kind = 'steer_refused' AND switch = %s",
        (_SW_NOSTEER,)).fetchone()[0]
    assert audit_fault == 1, (
        "audit.events steer_refused fault must be written for no-steer port")

    # ---- Assert (d): read_active_sentinels sees steered + kicked ports -------
    active = read_active_sentinels(flax_db.pool, grace_secs=30)
    assert (_SW_STEER, _PORT_STEER) in active, (
        "steered port must appear in active sentinels (flax-observe would freeze it)")
    assert (_SW_KICK, _PORT_KICK) in active, (
        "kicked port must appear in active sentinels (flax-observe would freeze it)")
    # Refused (no-steer) port must NOT have a sentinel — never wrote one.
    assert (_SW_NOSTEER, _PORT_NOSTEER) not in active, (
        "no-steer port must NOT appear in active sentinels (nothing was written)")
