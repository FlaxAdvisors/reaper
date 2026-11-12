"""Interrupted-flap self-heal (Task 3).

recover_stranded_ports() un-strands ports a killed flap left admin-down, using
two signals in precedence order:

  1. flap_pending markers (precise) -- a row written before a flap's shutdown
     and cleared after its no-shutdown; any survivor is a mid-flap port.
  2. an admin-down managed-access-port scan (fallback) -- because the persisted
     switch_facts.link field collapses 'disabled' into 'nolink', the scan alone
     can't distinguish a stranded port from an empty one, so it is conservative
     (managed access ports only, never trunk/uplink/no-steer/non-managed).

All tests are DB-free: the db boundary functions selfheal calls are stubbed and
the drivers are fakes that record set_admin_up calls. The audit-row writer and
the devices lookup (both of which touch pool.connection() directly) are stubbed
to no-ops / scripted answers so no Postgres is needed.
"""
import pytest

from flax_reconcile import selfheal


# --- fakes ------------------------------------------------------------------
class FakeDriver:
    """Records set_admin_up calls; optionally raises to simulate an unreachable
    switch so we can assert best-effort continuation."""

    def __init__(self, raises=False):
        self.raises = raises
        self.admin_up_calls = []

    def set_admin_up(self, port):
        self.admin_up_calls.append(port)
        if self.raises:
            from flax_reconcile.drivers import SwitchUnreachable
            raise SwitchUnreachable("simulated")


POOL = object()  # opaque token; all db access is stubbed


def _patch(monkeypatch, *, pending=None, sf_ports=None, desired=None,
           devices=None, recent_flaps=None):
    """Stub every db / side-effecting boundary selfheal touches.

    pending  -> read_flap_pending result (list of {switch, port, mac})
    sf_ports -> read_switch_facts_ports result ({(sw, port): fact})
    desired  -> read_desired_ports result (list of {switch, port})
    devices  -> set of (switch, port) that _resolves_to_device returns True for
    recent_flaps -> recent_unfinished_flap_ports result; None = every sf port
                 (i.e. reconcile itself left them shut)
    """
    monkeypatch.setattr(selfheal.db, "read_flap_pending",
                        lambda pool: list(pending or []))
    monkeypatch.setattr(selfheal.db, "read_switch_facts_ports",
                        lambda pool: dict(sf_ports or {}))
    monkeypatch.setattr(selfheal.db, "read_desired_ports",
                        lambda pool: list(desired or []))
    flaps = set(sf_ports or {}) if recent_flaps is None else set(recent_flaps)
    monkeypatch.setattr(selfheal.db, "recent_unfinished_flap_ports",
                        lambda pool, **kw: set(flaps))
    cleared = []
    monkeypatch.setattr(selfheal.db, "clear_flap_pending",
                        lambda pool, *, switch, port: cleared.append((switch, port)))
    audited = []
    monkeypatch.setattr(selfheal, "_audit_recovered",
                        lambda pool, *, switch, port, mac, via:
                        audited.append((switch, port, via)))
    dev = set(devices or set())
    monkeypatch.setattr(selfheal, "_resolves_to_device",
                        lambda pool, switch, port: (switch, port) in dev)
    return cleared, audited


# --- marker path ------------------------------------------------------------
def test_marker_recovers_pending_port(monkeypatch):
    cleared, audited = _patch(
        monkeypatch,
        pending=[{"switch": "rabbit-lorax", "port": "Ethernet8/3",
                  "mac": "aa:bb:cc:00:00:01"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 1
    assert drv.admin_up_calls == ["Ethernet8/3"]
    assert ("rabbit-lorax", "Ethernet8/3") in cleared
    assert ("rabbit-lorax", "Ethernet8/3", "flap_pending") in audited


def test_marker_failure_leaves_marker_for_retry(monkeypatch):
    """A switch unreachable during recovery: count stays 0, marker NOT cleared
    (so the next startup retries), and the pass does not crash."""
    cleared, _ = _patch(
        monkeypatch,
        pending=[{"switch": "rabbit-lorax", "port": "Ethernet8/3", "mac": None}])
    drv = FakeDriver(raises=True)
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 0
    assert ("rabbit-lorax", "Ethernet8/3") not in cleared


def test_marker_no_steer_skipped_and_cleared(monkeypatch):
    """A no-steer-listed port with a stale marker is NOT re-enabled (operator
    territory) but the stale marker is cleared so it doesn't linger."""
    cleared, _ = _patch(
        monkeypatch,
        pending=[{"switch": "rabbit-lorax", "port": "Ethernet1", "mac": None}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(
        POOL, {"rabbit-lorax": drv}, no_steer={("rabbit-lorax", "Ethernet1")})
    assert n == 0
    assert drv.admin_up_calls == []
    assert ("rabbit-lorax", "Ethernet1") in cleared


def test_marker_unmanaged_switch_marker_dropped(monkeypatch):
    """A marker for a switch no longer in `switches` is dropped, not acted on."""
    cleared, _ = _patch(
        monkeypatch,
        pending=[{"switch": "gone-switch", "port": "Ethernet1", "mac": None}])
    n = selfheal.recover_stranded_ports(POOL, {})  # no drivers
    assert n == 0
    assert ("gone-switch", "Ethernet1") in cleared


# --- fallback admin-down scan ----------------------------------------------
def test_scan_recovers_admin_down_managed_access_port(monkeypatch):
    """A managed (desired_port) access port reading not-up is re-enabled."""
    cleared, audited = _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet8/3"):
                  {"mask": "access", "access_vid": 17, "link": "nolink", "admin": "down"}},
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet8/3"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 1
    assert drv.admin_up_calls == ["Ethernet8/3"]
    assert ("rabbit-lorax", "Ethernet8/3", "admin_down_scan") in audited


def test_scan_recovers_port_resolving_to_device(monkeypatch):
    """A port with no desired_port row but a device location is still managed."""
    _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet9/1"):
                  {"mask": "access", "access_vid": 17, "link": "nolink", "admin": "down"}},
        devices={("rabbit-lorax", "Ethernet9/1")})
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 1
    assert drv.admin_up_calls == ["Ethernet9/1"]


def test_scan_leaves_link_up_port_alone(monkeypatch):
    """An already-up port (link='link') is never touched."""
    _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet8/3"):
                  {"mask": "access", "access_vid": 17, "link": "link"}},
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet8/3"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 0
    assert drv.admin_up_calls == []


def test_scan_recovers_nolink_managed_access_port(monkeypatch):
    """A managed access port reading the explicit admin-down 'nolink' state IS
    re-enabled (the genuine stranded-port case the scan must recover)."""
    _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet8/3"):
                  {"mask": "access", "access_vid": 17, "link": "nolink", "admin": "down"}},
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet8/3"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 1
    assert drv.admin_up_calls == ["Ethernet8/3"]


def test_scan_leaves_unknown_link_port_alone(monkeypatch):
    """A managed access port whose link reads 'unknown' (never polled / stale
    switch_facts) is NOT swept by the scan -- we don't `no shutdown` a port whose
    true state we don't know (could be an operator-disabled or un-polled port)."""
    _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet8/3"):
                  {"mask": "access", "access_vid": 17, "link": "unknown"}},
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet8/3"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 0
    assert drv.admin_up_calls == []


def test_scan_leaves_missing_link_port_alone(monkeypatch):
    """A managed access port with no link field at all (link=None) is likewise
    not swept -- only the explicit 'nolink' state is recovered by the scan."""
    _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet8/3"):
                  {"mask": "access", "access_vid": 17, "link": None}},
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet8/3"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 0
    assert drv.admin_up_calls == []


def test_scan_leaves_trunk_uplink_alone(monkeypatch):
    """A trunk/uplink port (mask='trunk') is never touched even if not-up."""
    _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet52"):
                  {"mask": "trunk", "access_vid": None, "link": "nolink", "admin": "down"}},
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet52"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 0
    assert drv.admin_up_calls == []


def test_scan_leaves_no_steer_port_alone(monkeypatch):
    """A no-steer-listed access port is never touched by the scan."""
    _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet1"):
                  {"mask": "access", "access_vid": 1, "link": "nolink", "admin": "down"}},
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet1"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(
        POOL, {"rabbit-lorax": drv}, no_steer={("rabbit-lorax", "Ethernet1")})
    assert n == 0
    assert drv.admin_up_calls == []


def test_scan_leaves_unmanaged_port_alone(monkeypatch):
    """An admin-down access port with neither a desired_port row nor a device
    location is NOT reconcile-managed -> left alone (don't fight an operator who
    disabled an infra port)."""
    _patch(
        monkeypatch,
        sf_ports={("rabbit-lorax", "Ethernet48"):
                  {"mask": "access", "access_vid": 1, "link": "nolink", "admin": "down"}},
        desired=[])  # not managed, no device
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    assert n == 0
    assert drv.admin_up_calls == []


def test_scan_skips_port_already_handled_by_marker(monkeypatch):
    """A port recovered via its marker is NOT re-issued in the scan."""
    _patch(
        monkeypatch,
        pending=[{"switch": "rabbit-lorax", "port": "Ethernet8/3", "mac": None}],
        sf_ports={("rabbit-lorax", "Ethernet8/3"):
                  {"mask": "access", "access_vid": 17, "link": "nolink", "admin": "down"}},
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet8/3"}])
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": drv})
    # Recovered exactly once (via the marker), not twice.
    assert n == 1
    assert drv.admin_up_calls == ["Ethernet8/3"]


def test_scan_best_effort_continues_past_unreachable(monkeypatch):
    """One unreachable port doesn't abort recovery of the next."""
    _patch(
        monkeypatch,
        sf_ports={
            ("rabbit-lorax", "Ethernet8/3"):
                {"mask": "access", "access_vid": 17, "link": "nolink", "admin": "down"},
            ("rabbit-lorax", "Ethernet8/4"):
                {"mask": "access", "access_vid": 17, "link": "nolink", "admin": "down"},
        },
        desired=[{"switch": "rabbit-lorax", "port": "Ethernet8/3"},
                 {"switch": "rabbit-lorax", "port": "Ethernet8/4"}])
    # First port raises, second succeeds.
    raising = FakeDriver(raises=True)
    # Use one driver instance but flip raises per call would be complex; instead
    # assert that with a raising driver the count is 0 but BOTH ports attempted.
    n = selfheal.recover_stranded_ports(POOL, {"rabbit-lorax": raising})
    assert n == 0
    assert set(raising.admin_up_calls) == {"Ethernet8/3", "Ethernet8/4"}


# --- narrowed scan (2026-11-06): admin-down AND reconcile left it shut -------
_SW, _P = "rabbit-lorax", "Ethernet8/3"


def _scan(monkeypatch, fact, recent):
    _patch(monkeypatch, sf_ports={(_SW, _P): fact},
           desired=[{"switch": _SW, "port": _P}], recent_flaps=recent)
    drv = FakeDriver()
    n = selfheal.recover_stranded_ports(POOL, {_SW: drv})
    return n, drv.admin_up_calls


def test_scan_recovers_admin_down_port_reconcile_shut(monkeypatch):
    n, calls = _scan(monkeypatch, {"mask": "access", "access_vid": 17,
                                   "link": "nolink", "admin": "down"}, {(_SW, _P)})
    assert (n, calls) == (1, [_P])


def test_scan_leaves_operator_shut_port_alone(monkeypatch):
    n, calls = _scan(monkeypatch, {"mask": "access", "access_vid": 17,
                                   "link": "nolink", "admin": "down"}, set())
    assert (n, calls) == (0, [])


def test_scan_leaves_link_down_only_port_alone(monkeypatch):
    n, calls = _scan(monkeypatch, {"mask": "access", "access_vid": 17,
                                   "link": "nolink"}, {(_SW, _P)})
    assert (n, calls) == (0, [])


def test_scan_ignores_port_without_admin_key(monkeypatch):
    for link in ("unknown", None):
        n, calls = _scan(monkeypatch, {"mask": "access", "access_vid": 17,
                                       "link": link}, {(_SW, _P)})
        assert (n, calls) == (0, [])
