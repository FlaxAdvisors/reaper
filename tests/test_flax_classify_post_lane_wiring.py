"""The post lane is invoked each cycle and isolated from the triage cycle."""
from unittest.mock import MagicMock

from flax_classify.post_reserve import observed_by_port


def test_observed_by_port_normalizes_join_key():
    rows = [{"switch": "rabbit-edam", "port": "Ethernet10/4",
             "resolved": {"bmc_mac": "98:03:9b:c6:4f:16", "source": "probe_confirmed"}}]
    got = observed_by_port(rows)
    assert ("rabbit-edam", "et10b4") in got
    assert got[("rabbit-edam", "et10b4")]["bmc_mac"] == "98:03:9b:c6:4f:16"


def test_run_post_reservations_threads_observed_into_derive(monkeypatch):
    """run_post_reservations must pass its `observed` kwarg through to
    derive_post_targets (the join half of the probe-verdict wiring)."""
    import flax_classify.post_reserve as pr

    captured = {}

    def _spy(facts, switch, **kw):
        captured.update(kw)
        return []
    monkeypatch.setattr(pr, "derive_post_targets", _spy)

    sentinel = {("rabbit-edam", "et10b4"): {"bmc_mac": "98:03:9b:c6:4f:16",
                                            "source": "probe_confirmed"}}
    pr.run_post_reservations(MagicMock(), order_no="RRORD-003",
                             racks=[("rabbit-edam", "")], facts={},
                             observed=sentinel)
    assert captured.get("observed") is sentinel


def test_post_lane_runs_after_cycle_and_is_isolated(monkeypatch):
    import flax_classify.__main__ as m

    order = {"calls": 0}
    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-003")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})

    def _fake_lane(pool, **kw):
        order["calls"] += 1
        return {"written": 2, "purged": 0, "derived_macs": frozenset()}
    monkeypatch.setattr(m, "run_post_reservations", _fake_lane)
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    monkeypatch.setattr(m, "reconcile_post_reservations",
                         lambda pool, **kw: {"deleted": 0, "timers": 0})

    # run_post_lane(pool, geometry_path, cycle_secs) is the thin wrapper _do_cycle calls.
    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert order["calls"] == 1


def test_post_lane_threads_derived_macs_into_reconcile(monkeypatch):
    """run_post_reservations' derived_macs must reach reconcile_post_reservations
    unchanged -- this is the wiring half of the keep-set-echo-deadlock fix."""
    import flax_classify.__main__ as m

    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-003")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "db_now", lambda pool: None)

    sentinel = frozenset({"aa:bb:cc:dd:ee:ff"})
    monkeypatch.setattr(m, "run_post_reservations",
                         lambda pool, **kw: {"written": 1, "purged": 0,
                                              "derived_macs": sentinel})
    captured = {}

    def _fake_reconcile(pool, **kw):
        captured.update(kw)
        return {"deleted": 0, "timers": 0}
    monkeypatch.setattr(m, "reconcile_post_reservations", _fake_reconcile)

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert captured["derived_macs"] == sentinel


def test_post_lane_derived_macs_defaults_empty_when_summary_lacks_key(monkeypatch):
    """The order_no-falsy no-op summary shape ({'written': 0, 'purged': 0}) has
    no 'derived_macs' key -- run_post_lane must not KeyError, and must pass an
    empty frozenset through instead."""
    import flax_classify.__main__ as m

    monkeypatch.setattr(m, "read_post_order", lambda pool: None)
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    monkeypatch.setattr(m, "run_post_reservations",
                         lambda pool, **kw: {"written": 0, "purged": 0})
    captured = {}

    def _fake_reconcile(pool, **kw):
        captured.update(kw)
        return {"deleted": 0, "timers": 0}
    monkeypatch.setattr(m, "reconcile_post_reservations", _fake_reconcile)

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert captured["derived_macs"] == frozenset()


def test_post_lane_swallows_errors(monkeypatch):
    import flax_classify.__main__ as m
    monkeypatch.setattr(m, "read_post_order", lambda pool: (_ for _ in ()).throw(RuntimeError("boom")))
    # must not raise
    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)


def test_post_lane_no_longer_threads_enforce_config(monkeypatch):
    """Post-3b demolition: run_post_reservations and reconcile_post_reservations
    have no legacy kea writers left to gate, so run_post_lane must not pass an
    `enforced`/`enforced_roles` kwarg into either call anymore."""
    import flax_classify.__main__ as m

    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-003")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "db_now", lambda pool: None)

    reserve_kwargs = {}

    def _fake_reserve(pool, **kw):
        reserve_kwargs.update(kw)
        return {"written": 1, "purged": 0, "derived_macs": frozenset()}
    monkeypatch.setattr(m, "run_post_reservations", _fake_reserve)

    reconcile_kwargs = {}

    def _fake_reconcile(pool, **kw):
        reconcile_kwargs.update(kw)
        return {"deleted": 0, "timers": 0}
    monkeypatch.setattr(m, "reconcile_post_reservations", _fake_reconcile)

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)

    assert "enforced" not in reserve_kwargs
    assert "enforced_roles" not in reserve_kwargs
    assert "enforced" not in reconcile_kwargs
    assert "enforced_roles" not in reconcile_kwargs


def test_post_lane_threads_role_defs_lifecycle_into_reconcile_cfg(monkeypatch):
    """Task 4: run_post_lane must source the reconcile cfg's lifecycle knobs
    from role_defs (registry-first), not env vars -- a role_defs carrying a
    distinctive linkdown_evict_secs must show up verbatim in the cfg
    threaded into reconcile_post_reservations."""
    import flax_classify.__main__ as m

    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-003")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    monkeypatch.setattr(m, "run_post_reservations",
                        lambda pool, **kw: {"written": 0, "purged": 0,
                                             "derived_macs": frozenset()})

    captured = {}

    def _fake_reconcile(pool, **kw):
        captured.update(kw)
        return {"deleted": 0, "timers": 0}
    monkeypatch.setattr(m, "reconcile_post_reservations", _fake_reconcile)

    role_defs = {"post": {"policy": {"lifecycle": {
        "linkdown_evict_secs": 321, "replace_debounce_cycles": 4}}}}
    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0, role_defs)

    assert captured["cfg"]["linkdown_evict_secs"] == 321.0
    assert captured["cfg"]["replace_debounce_secs"] == 120.0  # 4 cycles * 30s


def test_post_lane_defaults_role_defs_to_empty_when_omitted(monkeypatch):
    """Backwards-compatible default: callers (and older direct-call tests in
    this file) that don't pass role_defs still get the code-default
    lifecycle cfg (-1/2, Rule 3 fail-safe disabled), not a crash."""
    import flax_classify.__main__ as m

    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-003")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    monkeypatch.setattr(m, "run_post_reservations",
                        lambda pool, **kw: {"written": 0, "purged": 0,
                                             "derived_macs": frozenset()})

    captured = {}

    def _fake_reconcile(pool, **kw):
        captured.update(kw)
        return {"deleted": 0, "timers": 0}
    monkeypatch.setattr(m, "reconcile_post_reservations", _fake_reconcile)

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)

    assert captured["cfg"]["linkdown_evict_secs"] == -1.0
    assert captured["cfg"]["replace_debounce_secs"] == 60.0


def test_service_template_mounts_post_geometry_and_passes_arg():
    from pathlib import Path
    t = Path(__file__).resolve().parents[1] / \
        "roles/apply_flax_classify/templates/flax-classify.service.j2"
    body = t.read_text()
    assert "-v /etc/flax/post-geometry.json:/etc/flax/post-geometry.json:ro" in body
    assert "--post-geometry /etc/flax/post-geometry.json" in body


# ── sticky-slot purge must stick (et28b3 double-reservation, 2026-09-19) ────
#
# A blade swap at rabbit-edam et28b3 left TWO kea post reservations for
# 172.25.28.103: the departed BMC (X) and the new, present BMC (Y). Each pass
# run_post_reservations derived Y and its sticky-slot purge deleted X's
# desired row -- then reconcile_post_reservations' keep-set walked the ACTUAL
# kea rows, found X still there (not evicted: Rule 2 sees Y as reserved, not
# foreign) and re-upserted X's desired row from the stale kea row. The
# materializer then saw desired == actual for X and never deleted it. These
# tests drive the real run_post_lane over an in-memory desired_reservations
# store and assert the purge now sticks.

import datetime as _dt

_OLD_BMC = "98:03:9b:75:2a:b2"   # departed blade's BMC (X)
_NEW_BMC = "98:03:9b:a8:27:6e"   # new, present, comms-confirmed BMC (Y)
_OLD_NIC = "98:03:9b:75:2a:b0"   # departed blade's host nic
_NEW_NIC = "98:03:9b:a8:27:6c"   # new blade's host nic


def _kea_row(mac, kind, ipv4, hostname, operator_note=False):
    return {"mac": mac, "switch": "rabbit-edam", "port": "et28b3",
            "post": {}, "operator_note": operator_note, "kind": kind,
            "hostname": hostname, "vid": 25, "ipv4": ipv4, "ipv6": None}


class _DesiredStore:
    """In-memory desired_reservations (owner_role='post' only) + fakes for
    the three writer helpers post_reserve/post_reconcile call."""

    def __init__(self, rows):
        self.rows = {r["mac"]: dict(r) for r in rows}
        self.delete_calls = []

    def upsert_desired(self, pool, **k):
        self.rows[k["mac"].lower()] = {
            "mac": k["mac"].lower(), "owner_role": k["owner_role"],
            "kind": k["kind"], "hostname": k["hostname"], "ipv4": k["ipv4"],
            "ipv6": k["ipv6"], "vid": k["vid"], "switch": k["switch"],
            "port": k["port"]}

    def delete_desired(self, pool, *, owner_role, macs):
        self.delete_calls.append(list(macs))
        n = 0
        for m in macs:
            if self.rows.pop(m.lower(), None) is not None:
                n += 1
        return n

    def delete_desired_slot(self, pool, *, owner_role, switch, port, kind,
                            keep_mac, deleted_macs=None):
        victims = [m for m, r in self.rows.items()
                   if r["owner_role"] == owner_role and r["switch"] == switch
                   and r["port"] == port and r["kind"] == kind
                   and m != keep_mac.lower()]
        for m in victims:
            del self.rows[m]
        if deleted_macs is not None:
            deleted_macs.update(victims)
        return len(victims)


def _desired_from_kea(row):
    return {"mac": row["mac"], "owner_role": "post", "kind": row["kind"],
            "hostname": row["hostname"], "ipv4": row["ipv4"], "ipv6": None,
            "vid": 25, "switch": "rabbit-edam", "port": "et28b3"}


def _wire_lane(monkeypatch, *, store, kea_rows, port_fact, observed_resolved):
    import flax_classify.__main__ as m
    import flax_classify.post_reserve as pres
    import flax_classify.post_reconcile as prec

    for mod in (pres, prec):
        for name in ("upsert_desired", "delete_desired", "delete_desired_slot"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, getattr(store, name))
    monkeypatch.setattr(prec, "read_post_reservations", lambda pool: kea_rows)
    monkeypatch.setattr(prec, "stamp_post_timers", lambda pool, writes: None)

    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-003")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_post_slots", lambda path: {})
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {
        "rabbit-edam": {"ports": {"Ethernet28/3": port_fact}}})
    rows = ([{"switch": "rabbit-edam", "port": "Ethernet28/3",
              "resolved": observed_resolved}] if observed_resolved else [])
    monkeypatch.setattr(m, "read_observe_rows", lambda pool: rows)
    monkeypatch.setattr(m, "db_now", lambda pool: _dt.datetime(
        2026, 9, 19, 12, 0, 0, tzinfo=_dt.timezone.utc))
    monkeypatch.setattr(m, "read_kea_actuals", lambda pool: [])
    monkeypatch.setattr(m, "write_hosts_file", lambda path, records: None)
    return m


def _materializer_deletes(store, kea_rows):
    """Run the REAL pure materializer diff over the post-lane result: which
    post macs would it delete from kea.hosts?"""
    from flax_classify.materializer import plan_materialization
    actuals = [dict(r, source="post") for r in kea_rows]
    actions, _skipped = plan_materialization(
        list(store.rows.values()), actuals, {"post"})
    return {mac for owner, action, mac, _d in actions if action == "delete"}


def test_et28b3_swapped_bmc_stale_reservation_is_evicted(monkeypatch):
    kea = [_kea_row(_OLD_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_NEW_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc")]
    store = _DesiredStore([_desired_from_kea(r) for r in kea])
    m = _wire_lane(monkeypatch, store=store, kea_rows=kea,
                   port_fact={"link": "link", "mask": "access",
                              "access_vid": 25, "bmc_mac": _NEW_BMC,
                              "nic_macs": [], "macs": [_NEW_BMC]},
                   observed_resolved={"bmc_mac": _NEW_BMC,
                                      "source": "probe_confirmed"})

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)

    assert _NEW_BMC in store.rows
    assert _OLD_BMC not in store.rows          # purge stuck: no keep-set echo
    assert _materializer_deletes(store, kea) == {_OLD_BMC}

    # Next cycle: _do_cycle runs the materializer BEFORE run_post_lane, so
    # the stale kea row is gone by the next post pass -> steady state, the
    # old mac never comes back.
    kea[:] = [r for r in kea if r["mac"] != _OLD_BMC]
    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert _OLD_BMC not in store.rows
    assert _NEW_BMC in store.rows


def test_et28b3_swapped_host_nic_stale_reservation_is_evicted(monkeypatch):
    kea = [_kea_row(_NEW_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_OLD_NIC, "host", "172.25.28.3", "rr003-et28b3"),
           _kea_row(_NEW_NIC, "host", "172.25.28.3", "rr003-et28b3")]
    store = _DesiredStore([_desired_from_kea(r) for r in kea])
    m = _wire_lane(monkeypatch, store=store, kea_rows=kea,
                   port_fact={"link": "link", "mask": "access",
                              "access_vid": 25, "bmc_mac": _NEW_BMC,
                              "nic_macs": [_NEW_NIC],
                              "macs": [_NEW_BMC, _NEW_NIC]},
                   observed_resolved={"bmc_mac": _NEW_BMC, "nic_mac": _NEW_NIC,
                                      "source": "probe_confirmed"})

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)

    assert _NEW_NIC in store.rows and _NEW_BMC in store.rows
    assert _OLD_NIC not in store.rows
    assert _materializer_deletes(store, kea) == {_OLD_NIC}


def test_dark_bmc_mid_flash_nothing_purged_reservation_kept(monkeypatch):
    """Flash safety unchanged end-to-end: the reserved BMC is dark (not on
    the port, no comms-confirm) and NO new mac occupies the port -> nothing
    is derived, nothing purged, and the keep-set retains the reservation."""
    kea = [_kea_row(_OLD_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_OLD_NIC, "host", "172.25.28.3", "rr003-et28b3")]
    store = _DesiredStore([_desired_from_kea(r) for r in kea])
    m = _wire_lane(monkeypatch, store=store, kea_rows=kea,
                   port_fact={"link": "link", "mask": "access",
                              "access_vid": 25, "bmc_mac": None,
                              "nic_macs": [_OLD_NIC], "macs": [_OLD_NIC]},
                   observed_resolved=None)

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)

    assert _OLD_BMC in store.rows and _OLD_NIC in store.rows
    assert _materializer_deletes(store, kea) == set()


def test_post_lane_threads_purged_macs_into_reconcile(monkeypatch):
    import flax_classify.__main__ as m

    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-003")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    sentinel = frozenset({"98:03:9b:75:2a:b2"})
    monkeypatch.setattr(m, "run_post_reservations",
                        lambda pool, **kw: {"written": 1, "purged": 1,
                                             "derived_macs": frozenset(),
                                             "purged_macs": sentinel})
    captured = {}

    def _fake_reconcile(pool, **kw):
        captured.update(kw)
        return {"deleted": 0, "timers": 0}
    monkeypatch.setattr(m, "reconcile_post_reservations", _fake_reconcile)

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert captured["purged_macs"] == sentinel


def test_post_lane_purged_macs_defaults_empty_when_summary_lacks_key(monkeypatch):
    import flax_classify.__main__ as m

    monkeypatch.setattr(m, "read_post_order", lambda pool: None)
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    monkeypatch.setattr(m, "run_post_reservations",
                        lambda pool, **kw: {"written": 0, "purged": 0})
    captured = {}

    def _fake_reconcile(pool, **kw):
        captured.update(kw)
        return {"deleted": 0, "timers": 0}
    monkeypatch.setattr(m, "reconcile_post_reservations", _fake_reconcile)

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert captured["purged_macs"] == frozenset()


# ── Rule 2b wiring (spec 2026-09-19-post-replace-on-confirmed-bmc) ──────────

def test_post_lane_threads_observed_into_reconcile(monkeypatch):
    """The SAME observed dict the reserve pass gets must reach the reconcile."""
    import flax_classify.__main__ as m

    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-003")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_post_slots", lambda path: {})
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "read_observe_rows", lambda pool: [
        {"switch": "rabbit-edam", "port": "Ethernet28/3",
         "resolved": {"bmc_mac": _NEW_BMC, "source": "probe_confirmed"}}])
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    reserve_kw, recon_kw = {}, {}

    def _fake_reserve(pool, **kw):
        reserve_kw.update(kw)
        return {"written": 0, "purged": 0}
    monkeypatch.setattr(m, "run_post_reservations", _fake_reserve)

    def _fake_reconcile(pool, **kw):
        recon_kw.update(kw)
        return {"deleted": 0, "timers": 0}
    monkeypatch.setattr(m, "reconcile_post_reservations", _fake_reconcile)

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert recon_kw["observed"] == {("rabbit-edam", "et28b3"): {
        "bmc_mac": _NEW_BMC, "source": "probe_confirmed"}}
    assert recon_kw["observed"] is reserve_kw["observed"]


class _BrokenPurgeStore(_DesiredStore):
    """The sticky-slot purge fails every pass (it is try/except'd in
    run_post_reservations, so purged_macs stays empty). Proves Rule 2b alone
    evicts the departed occupant: correctness no longer rests on the purge."""

    def delete_desired_slot(self, pool, **k):
        raise RuntimeError("purge down")


def test_et28b3_2b_alone_evicts_swapped_bmc_and_stays_converged(monkeypatch):
    kea = [_kea_row(_OLD_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_NEW_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc")]
    store = _BrokenPurgeStore([_desired_from_kea(r) for r in kea])
    m = _wire_lane(monkeypatch, store=store, kea_rows=kea,
                   port_fact={"link": "link", "mask": "access",
                              "access_vid": 25, "bmc_mac": _NEW_BMC,
                              "nic_macs": [], "macs": [_NEW_BMC]},
                   observed_resolved={"bmc_mac": _NEW_BMC,
                                      "source": "probe_confirmed"})

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert _OLD_BMC not in store.rows and _NEW_BMC in store.rows
    assert _materializer_deletes(store, kea) == {_OLD_BMC}

    kea[:] = [r for r in kea if r["mac"] != _OLD_BMC]   # materializer ran
    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert _OLD_BMC not in store.rows and _NEW_BMC in store.rows
    assert _materializer_deletes(store, kea) == set()


def test_et28b3_2b_alone_evicts_departed_host_nic_with_its_bmc(monkeypatch):
    """D4, BMC-led: the old host nic goes in the same pass as the old BMC."""
    kea = [_kea_row(_OLD_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_NEW_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_OLD_NIC, "host", "172.25.28.3", "rr003-et28b3"),
           _kea_row(_NEW_NIC, "host", "172.25.28.3", "rr003-et28b3")]
    store = _BrokenPurgeStore([_desired_from_kea(r) for r in kea])
    m = _wire_lane(monkeypatch, store=store, kea_rows=kea,
                   port_fact={"link": "link", "mask": "access",
                              "access_vid": 25, "bmc_mac": _NEW_BMC,
                              "nic_macs": [_NEW_NIC],
                              "macs": [_NEW_BMC, _NEW_NIC]},
                   observed_resolved={"bmc_mac": _NEW_BMC, "nic_mac": _NEW_NIC,
                                      "source": "probe_confirmed"})

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert _OLD_BMC not in store.rows and _OLD_NIC not in store.rows
    assert _NEW_NIC in store.rows and _NEW_BMC in store.rows
    assert _materializer_deletes(store, kea) == {_OLD_BMC, _OLD_NIC}


def test_settled_slot_host_off_keeps_host_reservation_across_passes(monkeypatch):
    """The narrowed D4's reason: after the swap settles, the host is powered
    off and its nic ages out of the FDB while the BMC stays confirmed. The
    host reservation must survive every pass (no evict/re-add flap)."""
    kea = [_kea_row(_NEW_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_NEW_NIC, "host", "172.25.28.3", "rr003-et28b3")]
    store = _DesiredStore([_desired_from_kea(r) for r in kea])
    m = _wire_lane(monkeypatch, store=store, kea_rows=kea,
                   port_fact={"link": "link", "mask": "access",
                              "access_vid": 25, "bmc_mac": _NEW_BMC,
                              "nic_macs": [], "macs": [_NEW_BMC]},
                   observed_resolved={"bmc_mac": _NEW_BMC, "nic_mac": _NEW_NIC,
                                      "source": "probe_confirmed"})

    for _ in range(3):
        m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
        assert _NEW_NIC in store.rows and _NEW_BMC in store.rows
        assert _materializer_deletes(store, kea) == set()
    assert not [c for c in store.delete_calls if _NEW_NIC in c]


def test_et28b3_2b_and_purge_together_evict_old_once(monkeypatch):
    kea = [_kea_row(_OLD_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_NEW_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc")]
    store = _DesiredStore([_desired_from_kea(r) for r in kea])
    m = _wire_lane(monkeypatch, store=store, kea_rows=kea,
                   port_fact={"link": "link", "mask": "access",
                              "access_vid": 25, "bmc_mac": _NEW_BMC,
                              "nic_macs": [], "macs": [_NEW_BMC]},
                   observed_resolved={"bmc_mac": _NEW_BMC,
                                      "source": "probe_confirmed"})

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    flat = [mac for call in store.delete_calls for mac in call]
    assert flat.count(_OLD_BMC) == 1
    assert _materializer_deletes(store, kea) == {_OLD_BMC}


def test_et28b3_race_new_host_off_keeps_new_host(monkeypatch):
    """Final-review race (a): the new host nic is reserved but powered off
    (absent from the port's FDB) while the old bmc is still in kea and the
    new bmc is confirmed on the port. The SAME pass's reserve derives the
    new host nic fresh from observe's nic_mac; 2b must evict only the
    departed old bmc, never that freshly-derived new host reservation."""
    kea = [_kea_row(_OLD_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_NEW_BMC, "bmc", "172.25.28.103", "rr003-et28b3-bmc"),
           _kea_row(_NEW_NIC, "host", "172.25.28.3", "rr003-et28b3")]
    store = _DesiredStore([_desired_from_kea(r) for r in kea])
    m = _wire_lane(monkeypatch, store=store, kea_rows=kea,
                   port_fact={"link": "link", "mask": "access",
                              "access_vid": 25, "bmc_mac": _NEW_BMC,
                              "nic_macs": [], "macs": [_NEW_BMC]},
                   observed_resolved={"bmc_mac": _NEW_BMC, "nic_mac": _NEW_NIC,
                                      "source": "probe_confirmed"})

    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert _OLD_BMC not in store.rows
    assert _NEW_NIC in store.rows and _NEW_BMC in store.rows
    assert _materializer_deletes(store, kea) == {_OLD_BMC}

    kea[:] = [r for r in kea if r["mac"] != _OLD_BMC]   # materializer ran
    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert _OLD_BMC not in store.rows
    assert _NEW_NIC in store.rows and _NEW_BMC in store.rows
    assert _materializer_deletes(store, kea) == set()
    assert not [c for c in store.delete_calls if _NEW_NIC in c]


def test_post_lane_passes_node_orders(monkeypatch):
    import flax_classify.__main__ as m
    monkeypatch.setattr(m, "read_post_order", lambda pool: "RRORD-100")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [("rabbit-edam", "")])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {"rabbit-edam": {}})
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    monkeypatch.setattr(m, "read_post_assignments", lambda pool: {"b8599fabaf0e": "RRORD-1033"})
    seen = {}
    monkeypatch.setattr(m, "run_post_reservations",
                        lambda pool, **kw: seen.update(kw) or {"written": 0, "purged": 0})
    monkeypatch.setattr(m, "reconcile_post_reservations", lambda pool, **kw: {"deleted": 0, "timers": 0})
    m.run_post_lane(MagicMock(), "/etc/flax/post-geometry.json", 30.0)
    assert seen["node_orders"] == {"b8599fabaf0e": "RRORD-1033"}
