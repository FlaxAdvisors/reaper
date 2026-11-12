"""Pure plan_state_gc: orphan detection + anti-flap latch + guards (DB-free)."""
import datetime
from flax_post.observe import gc as gcmod
from flax_post.observe.gc import plan_state_gc, StateGcPlan

UTC = datetime.timezone.utc
def _now(**kw): return datetime.datetime(2026, 7, 2, 18, 0, tzinfo=UTC) + datetime.timedelta(**kw)

# switch_facts is arista-keyed; et6b1 -> Ethernet6/1
def _facts(macs): return {"Ethernet6/1": {"macs": list(macs)}}
def _state(bmc="aa:bb:cc:00:00:01", gc_gone_since=None, fw_phase=None):
    rec = {"port": "et6b1", "switch": "rabbit-edam", "bmc_mac": bmc, "serial": "SN"}
    if gc_gone_since is not None: rec["gc_gone_since"] = gc_gone_since
    if fw_phase is not None: rec["fw_bmc"] = {"phase": fw_phase}
    return {"et6b1": rec}

def test_orphan_stamps_latch_not_deleted():
    # unreserved, bmc gone from FDB (only a foreign mac present), not flashing
    p = plan_state_gc(_state(), set(), _facts(["ff:ff:ff:00:00:09"]), _now(), 300)
    assert p.deletes == []
    assert p.latch_writes == {"et6b1": _now().isoformat()}

def test_orphan_deleted_after_grace():
    p = plan_state_gc(_state(gc_gone_since=_now().isoformat()), set(),
                      _facts(["ff:ff:ff:00:00:09"]), _now(seconds=300), 300)
    assert p.deletes == ["et6b1"]

def test_orphan_within_grace_not_deleted():
    p = plan_state_gc(_state(gc_gone_since=_now().isoformat()), set(),
                      _facts(["ff:ff:ff:00:00:09"]), _now(seconds=299), 300)
    assert p.deletes == [] and p.latch_writes == {} and p.latch_clears == []

def test_reservation_returns_clears_latch():
    p = plan_state_gc(_state(gc_gone_since=_now().isoformat()), {"et6b1"},
                      _facts(["ff:ff:ff:00:00:09"]), _now(seconds=999), 300)
    assert p.deletes == [] and p.latch_clears == ["et6b1"]

def test_bmc_present_not_candidate():
    p = plan_state_gc(_state(gc_gone_since=_now().isoformat()), set(),
                      _facts(["aa:bb:cc:00:00:01"]), _now(seconds=999), 300)
    assert p.deletes == [] and p.latch_clears == ["et6b1"]

def test_flashing_never_candidate():
    p = plan_state_gc(_state(fw_phase="flashing"), set(),
                      _facts(["ff:ff:ff:00:00:09"]), _now(), 300)
    assert p.deletes == [] and p.latch_writes == {}

def test_reserved_off_blade_protected():
    # reserved (racked-but-off) with bmc gone -> never GC
    p = plan_state_gc(_state(), {"et6b1"}, _facts([]), _now(seconds=999), 300)
    assert p.deletes == [] and p.latch_writes == {}

def test_port_absent_from_switch_facts_never_candidate():
    p = plan_state_gc(_state(), set(), {}, _now(), 300)   # et6b1 not in facts
    assert p.deletes == [] and p.latch_writes == {}

def test_gc_post_state_unclaims_before_deleting_each_row(monkeypatch):
    """A blade pulled mid-boot leaves the ladder's claim sentinel behind; gc
    owns the row's whole lifetime, so it drops the claim before the delete."""
    unclaimed, deleted, set_calls = [], [], []
    monkeypatch.setattr(gcmod.claims, "unclaim", lambda port: unclaimed.append(port))
    monkeypatch.setattr(gcmod.state, "delete_state", lambda port: deleted.append(port))
    monkeypatch.setattr(gcmod.state, "set_state", lambda port, **kw: set_calls.append((port, kw)))
    states = {"et6b1": {"port": "et6b1", "bmc_mac": "aa:bb:cc:00:00:01",
                        "gc_gone_since": _now().isoformat()}}
    out = gcmod.gc_post_state(now=_now(seconds=999), states=states, reserved_ports=set(),
                              switch_facts=_facts(["ff:ff:ff:00:00:09"]), grace_secs=300)
    assert out["deleted"] == 1
    assert unclaimed == ["et6b1"] and deleted == ["et6b1"]


def test_gc_post_state_does_not_unclaim_rows_it_keeps(monkeypatch):
    unclaimed, deleted = [], []
    monkeypatch.setattr(gcmod.claims, "unclaim", lambda port: unclaimed.append(port))
    monkeypatch.setattr(gcmod.state, "delete_state", lambda port: deleted.append(port))
    monkeypatch.setattr(gcmod.state, "set_state", lambda port, **kw: None)
    gcmod.gc_post_state(now=_now(), states=_state(), reserved_ports={"et6b1"},
                        switch_facts=_facts([]), grace_secs=300)
    assert unclaimed == [] and deleted == []


# Link-down fast path (2026-09-28): classify's Rule 3 already debounced the
# link loss before it released the reservation, so a nolink candidate goes on
# this pass -- no second POST_STATE_GC_GRACE_SECS wait.
def _link_facts(link, macs=()):
    return {"Ethernet6/1": {"link": link, "macs": list(macs)}}

def test_nolink_orphan_deleted_on_first_pass():
    p = plan_state_gc(_state(), set(), _link_facts("nolink"), _now(), 300)
    assert p.deletes == ["et6b1"] and p.latch_writes == {}

def test_nolink_orphan_with_old_latch_deleted_before_grace():
    p = plan_state_gc(_state(gc_gone_since=_now().isoformat()), set(),
                      _link_facts("nolink"), _now(seconds=1), 300)
    assert p.deletes == ["et6b1"]

def test_nolink_still_reserved_kept():
    # Rule 3 has not released it yet -> the debounce is still running
    p = plan_state_gc(_state(), {"et6b1"}, _link_facts("nolink"), _now(), 300)
    assert p.deletes == [] and p.latch_writes == {}

def test_nolink_flashing_kept():
    p = plan_state_gc(_state(fw_phase="flashing"), set(), _link_facts("nolink"), _now(), 300)
    assert p.deletes == [] and p.latch_writes == {}

def test_unknown_link_takes_latch_path():
    # switch answered 'unknown' -- not a reported loss, keep the full grace
    p = plan_state_gc(_state(), set(), _link_facts("unknown"), _now(), 300)
    assert p.deletes == [] and p.latch_writes == {"et6b1": _now().isoformat()}

def test_link_up_bmc_gone_takes_latch_path():
    p = plan_state_gc(_state(), set(), _link_facts("link", ["ff:ff:ff:00:00:09"]), _now(), 300)
    assert p.deletes == [] and p.latch_writes == {"et6b1": _now().isoformat()}
