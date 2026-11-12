"""Pure-function tests for flax_classify.post_reconcile.plan_post_reconcile.

DB-free: the plan is computed from plain dicts + a synthetic `now`.
"""
import datetime

from flax_classify.post_reconcile import plan_post_reconcile, ReconcilePlan

UTC = datetime.timezone.utc


def _now(**kw):
    return datetime.datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC) + datetime.timedelta(**kw)


def _cfg(linkdown=900.0, replace=60.0, flash=None):
    return {"linkdown_evict_secs": linkdown,
            "replace_debounce_secs": replace,
            "flash_macs": flash or set()}


def _facts(link="link", macs=("aa:aa:aa:aa:aa:aa",)):
    return {"rabbit-edam": {"ports": {"Ethernet6/1": {"link": link, "macs": list(macs)}}}}


def _res(mac="aa:aa:aa:aa:aa:aa", post=None, operator_note=False,
         kind=None, hostname=None, vid=None, ipv4=None, ipv6=None):
    return {"mac": mac, "switch": "rabbit-edam", "port": "et6b1",
            "post": post or {}, "operator_note": operator_note,
            "kind": kind, "hostname": hostname, "vid": vid,
            "ipv4": ipv4, "ipv6": ipv6}


# --- healthy: mac present, link up -> nothing to do, timers cleared ---
def test_healthy_port_no_changes():
    plan = plan_post_reconcile([_res()], _facts(), _now(), _cfg())
    assert plan.deletes == []
    assert plan.timer_writes == {}


def test_healthy_clears_stale_timers():
    r = _res(post={"link_down_since": _now(seconds=-10).isoformat(),
                   "mac_absent_since": _now(seconds=-10).isoformat()})
    plan = plan_post_reconcile([r], _facts(), _now(), _cfg())
    assert plan.deletes == []
    assert plan.timer_writes == {"aa:aa:aa:aa:aa:aa": {}}


# --- Rule 3: link down ---
def test_linkdown_stamps_then_evicts():
    # first sighting: stamp, no delete
    p1 = plan_post_reconcile([_res()], _facts(link="nolink", macs=()), _now(), _cfg())
    assert p1.deletes == []
    assert p1.timer_writes["aa:aa:aa:aa:aa:aa"]["link_down_since"] == _now().isoformat()
    # 15 min later: evict
    r = _res(post={"link_down_since": _now().isoformat()})
    p2 = plan_post_reconcile([r], _facts(link="nolink", macs=()), _now(seconds=900), _cfg())
    assert p2.deletes == ["aa:aa:aa:aa:aa:aa"]


def test_linkdown_cleared_when_link_returns():
    r = _res(post={"link_down_since": _now(seconds=-100).isoformat()})
    plan = plan_post_reconcile([r], _facts(link="link"), _now(), _cfg())
    assert plan.deletes == []
    assert plan.timer_writes == {"aa:aa:aa:aa:aa:aa": {}}


def test_unknown_link_does_not_advance_or_evict():
    # switch unreachable -> port row absent entirely
    r = _res(post={"link_down_since": _now(seconds=-5000).isoformat()})
    plan = plan_post_reconcile([r], {"rabbit-edam": {"ports": {}}}, _now(), _cfg())
    assert plan.deletes == []
    assert plan.timer_writes == {}  # untouched


def test_linkdown_disabled_by_nonpositive_secs():
    r = _res(post={"link_down_since": _now(seconds=-99999).isoformat()})
    plan = plan_post_reconcile([r], _facts(link="nolink", macs=()), _now(), _cfg(linkdown=0))
    assert plan.deletes == []


# --- Rule 2: replaced (foreign mac present) ---
def test_replace_stamps_then_evicts_with_foreign_mac():
    facts = _facts(link="link", macs=("bb:bb:bb:bb:bb:bb",))  # foreign occupant
    p1 = plan_post_reconcile([_res()], facts, _now(), _cfg())
    assert p1.deletes == []
    assert p1.timer_writes["aa:aa:aa:aa:aa:aa"]["mac_absent_since"] == _now().isoformat()
    r = _res(post={"mac_absent_since": _now().isoformat()})
    p2 = plan_post_reconcile([r], facts, _now(seconds=60), _cfg())
    assert p2.deletes == ["aa:aa:aa:aa:aa:aa"]


def test_no_evict_when_absent_but_no_foreign_mac_flash_safety():
    # BMC (aa) dark during flash; only the host reservation (cc) remains -> no foreign mac
    res = [_res(mac="aa:aa:aa:aa:aa:aa"),
           {"mac": "cc:cc:cc:cc:cc:cc", "switch": "rabbit-edam", "port": "et6b1",
            "post": {}, "operator_note": False}]
    facts = _facts(link="link", macs=("cc:cc:cc:cc:cc:cc",))
    plan = plan_post_reconcile(res, facts, _now(seconds=999), _cfg())
    assert plan.deletes == []  # aa never evicted despite long absence


def test_unknown_link_with_present_port_row_does_not_touch_mac_absent_since():
    # Port row IS present (switch reachable) with a foreign mac on it, but link
    # itself reads "unknown". A stamped mac_absent_since debounce must survive
    # untouched (not cleared, not advanced, no evict) while link == "unknown" --
    # distinct from test_unknown_link_does_not_advance_or_evict, which covers
    # the case where the port row is absent entirely.
    facts = _facts(link="unknown", macs=("bb:bb:bb:bb:bb:bb",))  # foreign mac present
    r = _res(post={"mac_absent_since": _now(seconds=-30).isoformat()})
    plan = plan_post_reconcile([r], facts, _now(), _cfg())
    assert plan.deletes == []
    assert plan.timer_writes == {}  # untouched: not cleared, not advanced


def test_flash_macs_suppresses():
    facts = _facts(link="link", macs=("bb:bb:bb:bb:bb:bb",))
    r = _res(post={"mac_absent_since": _now(seconds=-999).isoformat()})
    plan = plan_post_reconcile([r], facts, _now(), _cfg(flash={"aa:aa:aa:aa:aa:aa"}))
    assert plan.deletes == []


# --- Task 6: probe-confirmed replacement supersede + evict interaction ---
def test_probe_confirmed_replacement_supersedes_and_evicts_old():
    """A probe-confirmed replacement blade evicts the departed identity through
    the SAME Rule 2 foreign-MAC gate a switch_facts-derived replacement uses --
    the purge operates on desired rows regardless of how the new identity was
    derived (probe vs arithmetic). Two halves:

      (a/b/c) OLD source='post' bmc reservation (aa) on a port whose switch_facts
      now report link='link' with aa ABSENT and a NEW foreign mac (bb) PRESENT
      (foreign = not itself a post reservation for that port); mac_absent_since
      already past replace_debounce -> Rule 2 evicts aa.

      flash-safety: a still-present reservation (aa) whose own mac is transiently
      absent while the ONLY other mac on the port is itself a post reservation
      (cc, not foreign) is NOT evicted, even after a long absence.
    """
    # (a) old post bmc reservation; (c) debounce already exceeded via an old
    # mac_absent_since (default replace=60s, absence=999s -> past window).
    old = _res(mac="aa:aa:aa:aa:aa:aa", kind="bmc",
               post={"mac_absent_since": _now(seconds=-999).isoformat()})
    # (b) port up, old mac absent, a NEW foreign mac occupies the port.
    facts = _facts(link="link", macs=("bb:bb:bb:bb:bb:bb",))
    plan = plan_post_reconcile([old], facts, _now(), _cfg())
    assert plan.deletes == ["aa:aa:aa:aa:aa:aa"]  # Rule 2 foreign-mac eviction

    # flash-safety: only other mac on the port (cc) is itself a reservation, so
    # no foreign occupant -> aa is NOT evicted despite the long absence.
    safe = [_res(mac="aa:aa:aa:aa:aa:aa", kind="bmc",
                 post={"mac_absent_since": _now(seconds=-999).isoformat()}),
            {"mac": "cc:cc:cc:cc:cc:cc", "switch": "rabbit-edam", "port": "et6b1",
             "post": {}, "operator_note": False}]
    safe_facts = _facts(link="link", macs=("cc:cc:cc:cc:cc:cc",))
    safe_plan = plan_post_reconcile(safe, safe_facts, _now(), _cfg())
    assert safe_plan.deletes == []


# --- guards ---
def test_operator_note_never_touched():
    r = _res(post={"link_down_since": _now(seconds=-99999).isoformat()}, operator_note=True)
    plan = plan_post_reconcile([r], _facts(link="nolink", macs=()), _now(), _cfg())
    assert plan.deletes == []
    assert plan.timer_writes == {}


from unittest import mock


def test_reconcile_reads_plans_and_applies():
    """Post-3b demolition: reconcile_post_reservations no longer calls a
    legacy kea deleter -- `deleted` in the summary is now the count of the
    plan's own eviction decisions (len(plan.deletes))."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="nolink", macs=())
    res = [_res(post={"link_down_since": _now().isoformat()})]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers") as stamp, \
         mock.patch.object(pr, "delete_desired"):
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(seconds=900), cfg=_cfg())
    assert out == {"deleted": 1, "timers": 0}
    stamp.assert_not_called()  # nothing to stamp when the only row is deleted


def test_reconcile_mirrors_deletes_to_desired_reservations():
    """reconcile's eviction decision is mirrored into desired_reservations
    via delete_desired(pool, owner_role="post", macs=macs) -- same macs
    list as plan.deletes."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="nolink", macs=())
    res = [_res(post={"link_down_since": _now().isoformat()})]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(seconds=900), cfg=_cfg())
    assert out == {"deleted": 1, "timers": 0}
    delete_desired.assert_called_once_with(
        mock.ANY, owner_role="post", macs=["aa:aa:aa:aa:aa:aa"])


def test_reconcile_desired_write_failure_isolated():
    """A raising delete_desired must not stop reconcile_post_reservations
    from returning its normal summary -- write failures never break the
    lane."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="nolink", macs=())
    res = [_res(post={"link_down_since": _now().isoformat()})]

    def _boom(pool, **kw):
        raise RuntimeError("desired_reservations table missing (pre-migration)")

    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired", _boom):
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(seconds=900), cfg=_cfg())
    assert out == {"deleted": 1, "timers": 0}


def test_reconcile_no_desired_delete_call_when_nothing_to_delete():
    """No deletes planned -> delete_desired is never called."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=("aa:aa:aa:aa:aa:aa",))
    res = [_res()]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg())
    assert out == {"deleted": 0, "timers": 0}
    delete_desired.assert_not_called()
    upsert_desired.assert_called_once_with(
        mock.ANY, owner_role="post", mac="aa:aa:aa:aa:aa:aa", kind="bmc",
        hostname=None, ipv4=None, ipv6=None, vid=None,
        switch="rabbit-edam", port="et6b1")


# --- Task 3: post keep-set desired emission ---

def test_reconcile_keepset_upserts_retained_reservation():
    """A retained reservation (not in plan.deletes) is upserted into
    desired_reservations with owner_role='post' and its own field values."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=("aa:aa:aa:aa:aa:aa",))
    res = [_res(kind="bmc", hostname="et6b1-bmc", vid=31,
                ipv4="172.31.6.101", ipv6="fe80::1")]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg())
    assert out == {"deleted": 0, "timers": 0}
    delete_desired.assert_not_called()
    upsert_desired.assert_called_once_with(
        mock.ANY, owner_role="post", mac="aa:aa:aa:aa:aa:aa", kind="bmc",
        hostname="et6b1-bmc", ipv4="172.31.6.101", ipv6="fe80::1", vid=31,
        switch="rabbit-edam", port="et6b1")


def test_reconcile_keepset_excludes_deleted_rows():
    """A row IN plan.deletes is NOT keep-set-upserted, but is still
    delete_desired-mirrored (existing behavior, unchanged)."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="nolink", macs=())
    res = [_res(post={"link_down_since": _now().isoformat()})]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(seconds=900), cfg=_cfg())
    assert out == {"deleted": 1, "timers": 0}
    upsert_desired.assert_not_called()
    delete_desired.assert_called_once_with(
        mock.ANY, owner_role="post", macs=["aa:aa:aa:aa:aa:aa"])


def test_reconcile_keepset_upsert_failure_isolated_delete_mirror_still_applied():
    """upsert_desired raising during the keep-set pass must not stop the
    reconcile from completing, and the delete_desired mirror for a DIFFERENT
    (evicted) row must still have run (they execute before the keep-set
    block)."""
    from flax_classify import post_reconcile as pr
    facts = {"rabbit-edam": {"ports": {
        "Ethernet6/1": {"link": "link", "macs": ["aa:aa:aa:aa:aa:aa"]},
        "Ethernet6/2": {"link": "nolink", "macs": []},
    }}}
    res = [
        _res(mac="aa:aa:aa:aa:aa:aa"),  # retained
        {"mac": "bb:bb:bb:bb:bb:bb", "switch": "rabbit-edam", "port": "et6b2",
         "post": {"link_down_since": _now().isoformat()}, "operator_note": False,
         "kind": "bmc", "hostname": None, "vid": None, "ipv4": None, "ipv6": None},
    ]

    def _boom(pool, **kw):
        raise RuntimeError("desired_reservations table missing (pre-migration)")

    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired", _boom):
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(seconds=900), cfg=_cfg())
    assert out == {"deleted": 1, "timers": 0}
    delete_desired.assert_called_once_with(
        mock.ANY, owner_role="post", macs=["bb:bb:bb:bb:bb:bb"])


def test_reconcile_stamp_post_timers_always_runs_when_only_timers_planned():
    """A reservation whose plan is a timer stamp (not a delete) must have
    stamp_post_timers called -- the metadata exception is unconditional,
    independent of anything else in the function."""
    from flax_classify import post_reconcile as pr

    facts = _facts(link="nolink", macs=())  # first nolink sighting -> stamp only
    res = [_res()]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers") as stamp, \
         mock.patch.object(pr, "delete_desired"), \
         mock.patch.object(pr, "upsert_desired"):
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg())
    assert out == {"deleted": 0, "timers": 1}
    stamp.assert_called_once_with(
        mock.ANY, {"aa:aa:aa:aa:aa:aa": {"link_down_since": _now().isoformat()}})


def test_reconcile_keepset_kind_none_defaults_to_bmc():
    """A legacy row with kind=None (predates the classify ctx kind key)
    upserts with kind='bmc' as the fallback."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=("aa:aa:aa:aa:aa:aa",))
    res = [_res(kind=None)]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired"), \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg())
    assert out == {"deleted": 0, "timers": 0}
    upsert_desired.assert_called_once_with(
        mock.ANY, owner_role="post", mac="aa:aa:aa:aa:aa:aa", kind="bmc",
        hostname=None, ipv4=None, ipv6=None, vid=None,
        switch="rabbit-edam", port="et6b1")


# --- final-review fix: keep-set must skip macs the reserve pass just derived ---

def test_reconcile_keepset_skips_mac_the_reserve_pass_just_derived():
    """A retained reservation whose mac is in derived_macs (this cycle's
    run_post_reservations already upserted a FRESH desired row for it) must
    NOT be re-upserted by the keep-set pass -- otherwise the keep-set's
    actual-kea-row echo permanently clobbers the fresh derivation."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=("aa:aa:aa:aa:aa:aa",))
    res = [_res(kind="bmc", hostname="et6b1-bmc", vid=31,
                ipv4="172.31.6.101", ipv6="fe80::1")]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired"), \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg(),
            derived_macs=frozenset({"aa:aa:aa:aa:aa:aa"}))
    assert out == {"deleted": 0, "timers": 0}
    upsert_desired.assert_not_called()


def test_reconcile_keepset_still_upserts_quiet_port_mac_not_in_derived_macs():
    """A retained reservation for a mac NOT in derived_macs (a quiet port the
    reserve pass no longer derives a target for this cycle) is still
    keep-set-upserted -- derived_macs narrows the skip to exactly the macs
    the reserve pass refreshed, not the whole retained set."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=("aa:aa:aa:aa:aa:aa",))
    res = [_res(kind="bmc", hostname="et6b1-bmc", vid=31,
                ipv4="172.31.6.101", ipv6="fe80::1")]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired"), \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg(),
            derived_macs=frozenset({"bb:bb:bb:bb:bb:bb"}))
    assert out == {"deleted": 0, "timers": 0}
    upsert_desired.assert_called_once_with(
        mock.ANY, owner_role="post", mac="aa:aa:aa:aa:aa:aa", kind="bmc",
        hostname="et6b1-bmc", ipv4="172.31.6.101", ipv6="fe80::1", vid=31,
        switch="rabbit-edam", port="et6b1")


def test_reconcile_default_derived_macs_empty_keeps_prior_behavior():
    """Calling without derived_macs at all (the pre-fix call shape) keeps the
    keep-set unconditional -- empty frozenset default skips nothing."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=("aa:aa:aa:aa:aa:aa",))
    res = [_res()]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired"), \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg())
    upsert_desired.assert_called_once()


def test_reconcile_keepset_per_row_isolation_one_failure_does_not_block_others():
    """The keep-set loop isolates each row's upsert_desired in its OWN
    try/except, so one mac's failure does not skip the remaining macs'
    keep-set upserts this cycle."""
    from flax_classify import post_reconcile as pr
    facts = {"rabbit-edam": {"ports": {
        "Ethernet6/1": {"link": "link", "macs": ["aa:aa:aa:aa:aa:aa"]},
        "Ethernet6/2": {"link": "link", "macs": ["cc:cc:cc:cc:cc:cc"]},
    }}}
    res = [
        _res(mac="aa:aa:aa:aa:aa:aa"),
        {"mac": "cc:cc:cc:cc:cc:cc", "switch": "rabbit-edam", "port": "et6b2",
         "post": {}, "operator_note": False,
         "kind": "bmc", "hostname": None, "vid": None, "ipv4": None, "ipv6": None},
    ]
    calls = []

    def _upsert(pool, **kw):
        if kw["mac"] == "aa:aa:aa:aa:aa:aa":
            raise RuntimeError("boom")
        calls.append(kw["mac"])

    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired"), \
         mock.patch.object(pr, "upsert_desired", _upsert):
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg())
    assert out == {"deleted": 0, "timers": 0}
    # the SECOND row's upsert must still have run despite the first raising.
    assert calls == ["cc:cc:cc:cc:cc:cc"]


# ── Post-3b demolition: no kea_hosts writer calls, ever ─────────────────────

# --- sticky-slot purge must stick: purged_macs (et28b3, 2026-09-19) ---
#
# Port et6b1 now holds the new BMC (bb, reserved + present); the old BMC (aa)
# is still a kea post reservation but run_post_reservations' sticky-slot purge
# deleted its desired row this pass (purged_macs). Rule 2 does not evict aa
# (bb is reserved, not foreign), so without purged_macs the keep-set echoes
# aa's stale kea row straight back into desired_reservations.

_OLD, _NEW = "aa:aa:aa:aa:aa:aa", "bb:bb:bb:bb:bb:bb"


def _swap_res(old_note=False):
    return [_res(mac=_OLD, kind="bmc", operator_note=old_note),
            _res(mac=_NEW, kind="bmc")]


def _run_swap(purged, derived=frozenset(), cfg=None, old_note=False):
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=(_NEW,))
    with mock.patch.object(pr, "read_post_reservations",
                           return_value=_swap_res(old_note)), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=cfg or _cfg(),
            derived_macs=derived, purged_macs=purged)
    upserted = {c.kwargs["mac"] for c in upsert_desired.call_args_list}
    deleted = {m for c in delete_desired.call_args_list
               for m in c.kwargs["macs"]}
    return out, upserted, deleted, delete_desired


def test_reconcile_purged_mac_skipped_by_keepset_and_evicted():
    out, upserted, deleted, delete_desired = _run_swap(
        purged=frozenset({_OLD}), derived=frozenset({_NEW}))
    assert _OLD not in upserted            # no stale kea-row echo
    assert deleted == {_OLD}               # evicted from desired (owner post)
    for c in delete_desired.call_args_list:
        assert c.kwargs["owner_role"] == "post"
    assert out["deleted"] == 1


def test_reconcile_purged_mac_with_operator_note_is_kept():
    out, upserted, deleted, _ = _run_swap(
        purged=frozenset({_OLD}), derived=frozenset({_NEW}), old_note=True)
    assert _OLD in upserted                # keep-set restores it, as before
    assert _OLD not in deleted
    assert out["deleted"] == 0


def test_reconcile_purged_mac_in_flash_macs_is_kept():
    out, upserted, deleted, _ = _run_swap(
        purged=frozenset({_OLD}), derived=frozenset({_NEW}),
        cfg=_cfg(flash={_OLD.upper()}))
    assert _OLD in upserted
    assert _OLD not in deleted
    assert out["deleted"] == 0


def test_reconcile_purged_and_rederived_mac_is_not_deleted():
    """A mac purged by one slot's claim but re-derived (fresh desired row)
    in the same pass must never be evicted."""
    out, upserted, deleted, _ = _run_swap(
        purged=frozenset({_OLD}), derived=frozenset({_OLD, _NEW}))
    assert _OLD not in deleted
    assert _OLD not in upserted            # derived -> keep-set skips as before
    assert out["deleted"] == 0


def test_reconcile_default_purged_macs_empty_keeps_prior_behavior():
    """No purged_macs (old call shape): the old mac is kept exactly as
    before -- the dark-BMC flash-safety branch never evicts it."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=(_NEW,))
    with mock.patch.object(pr, "read_post_reservations",
                           return_value=_swap_res()), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg())
    assert out == {"deleted": 0, "timers": 0}
    delete_desired.assert_not_called()
    assert {c.kwargs["mac"] for c in upsert_desired.call_args_list} == {_OLD, _NEW}


def test_reconcile_purged_mac_evict_failure_isolated():
    """A delete_desired failure for the purged-mac eviction never breaks the
    reconcile nor the remaining keep-set upserts."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=(_NEW,))
    with mock.patch.object(pr, "read_post_reservations",
                           return_value=_swap_res()), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired",
                           side_effect=RuntimeError("boom")), \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg(),
            purged_macs=frozenset({_OLD}))
    assert {c.kwargs["mac"] for c in upsert_desired.call_args_list} == {_NEW}
    assert "deleted" in out


# --- Rule 2b: a comms-confirmed new BMC evicts the slot's departed
# reservations on the first pass (spec 2026-09-19-post-replace-on-confirmed-bmc).
import logging

import pytest

_A, _B = "aa:aa:aa:aa:aa:aa", "bb:bb:bb:bb:bb:bb"   # old bmc, new bmc
_C, _D = "cc:cc:cc:cc:cc:cc", "dd:dd:dd:dd:dd:dd"   # old host nic, present host nic


def _obs(bmc_mac=_B, source="probe_confirmed"):
    return {("rabbit-edam", "et6b1"): {"bmc_mac": bmc_mac, "source": source}}


def test_2b_et28b3_departed_bmc_evicted_first_pass_no_timer():
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == [_A]
    assert plan.timer_writes == {}   # Rule 2 did not also stamp mac_absent_since


def test_2b_new_bmc_already_reserved_race_still_evicts_old():
    res = [_res(mac=_A, kind="bmc"), _res(mac=_B, kind="bmc")]
    plan = plan_post_reconcile(res, _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == [_A]
    assert plan.timer_writes == {}


def test_2b_probe_promote_bmc_counts_as_confirmed():
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs(source="probe_promote_bmc"))
    assert plan.deletes == [_A]


def test_2b_whole_chassis_departed_host_evicted_present_host_kept():
    res = [_res(mac=_A, kind="bmc"), _res(mac=_C, kind="host"),
           _res(mac=_D, kind="host")]
    plan = plan_post_reconcile(res, _facts(macs=(_B, _D)), _now(), _cfg(),
                               observed=_obs())
    assert sorted(plan.deletes) == [_A, _C]
    assert _D not in plan.timer_writes


def test_2b_host_off_on_settled_slot_keeps_host_reservation():
    # After the swap: new bmc bb and its host nic dd both reserved. Host is
    # powered off, so dd aged out of the FDB. No bmc reservation is evicted
    # at this slot, so dd must NOT be evicted (it would flap every cycle).
    res = [_res(mac=_B, kind="bmc"), _res(mac=_D, kind="host")]
    plan = plan_post_reconcile(res, _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == []


def test_2b_host_never_evicted_by_itself():
    # Old bmc already cleared earlier; old host nic cc lingers, absent.
    res = [_res(mac=_B, kind="bmc"), _res(mac=_C, kind="host")]
    plan = plan_post_reconcile(res, _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == []


def test_2b_host_kept_when_old_bmc_is_operator_noted():
    res = [_res(mac=_A, kind="bmc", operator_note=True),
           _res(mac=_C, kind="host")]
    plan = plan_post_reconcile(res, _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == []


def test_2b_flash_safety_only_host_on_port_observe_names_old_bmc():
    # BMC aa dark mid-flash: only its host nic cc is on the port; observe's
    # latch still names aa itself. Nothing is evicted.
    res = [_res(mac=_A, kind="bmc"), _res(mac=_C, kind="host")]
    plan = plan_post_reconcile(res, _facts(macs=(_C,)), _now(seconds=999),
                               _cfg(), observed=_obs(bmc_mac=_A))
    assert plan.deletes == []


@pytest.mark.parametrize("observed", [
    _obs(source="heuristic"),
    _obs(source="probe_flip_host"),
    {("rabbit-edam", "et6b1"): {}},
    {},
], ids=["heuristic", "probe_flip_host", "empty-resolved", "no-row"])
def test_2b_needs_comms_confirmation(observed):
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(macs=(_B,)), _now(), _cfg(),
                               observed=observed)
    assert plan.deletes == []
    # Rule 2 still runs for what 2b leaves: bb is foreign, so the timer stamps.
    assert plan.timer_writes[_A]["mac_absent_since"] == _now().isoformat()


def test_2b_stale_observe_latch_not_on_port_does_not_evict():
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs(bmc_mac="ee:ee:ee:ee:ee:ee"))
    assert plan.deletes == []


def test_2b_reserved_mac_still_on_port_not_evicted():
    # Old BMC present again (blade reseated) alongside a confirmed bb.
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(macs=(_A, _B)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == []


@pytest.mark.parametrize("link", ["nolink", "unknown"])
def test_2b_requires_link_up(link):
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(link=link, macs=(_B,)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == []


def test_2b_null_kind_not_evicted():
    plan = plan_post_reconcile([_res(mac=_A, kind=None)],
                               _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == []
    assert "mac_absent_since" in plan.timer_writes[_A]   # left to Rule 2


def test_2b_operator_note_never_evicted():
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc", operator_note=True)],
                               _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs())
    assert plan.deletes == []
    assert plan.timer_writes == {}


def test_2b_flash_macs_never_evicted():
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(macs=(_B,)), _now(), _cfg(flash={_A}),
                               observed=_obs())
    assert plan.deletes == []


def test_2b_mac_normalisation_across_formats():
    # Reservation colon-lower, port facts upper-colon, observe dash-upper.
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(macs=(_B.upper(),)), _now(), _cfg(),
                               observed=_obs(bmc_mac=_B.upper().replace(":", "-")))
    assert plan.deletes == [_A]


def test_2b_malformed_observe_mac_is_ignored_not_raised():
    plan = plan_post_reconcile([_res(mac=_A, kind="bmc")],
                               _facts(macs=(_B,)), _now(), _cfg(),
                               observed=_obs(bmc_mac="not-a-mac"))
    assert plan.deletes == []


def test_2b_observed_none_is_todays_plan():
    args = ([_res(mac=_A, kind="bmc"), _res(mac=_B, kind="bmc")],
            _facts(macs=(_B,)), _now(), _cfg())
    assert plan_post_reconcile(*args, observed=None) == plan_post_reconcile(*args)
    assert plan_post_reconcile(*args).deletes == []   # the et28b3 bug, without 2b


def test_2b_logs_one_info_line_per_eviction(caplog):
    caplog.set_level(logging.INFO, logger="flax_classify.post_reconcile")
    plan_post_reconcile([_res(mac=_A, kind="bmc")], _facts(macs=(_B,)),
                        _now(), _cfg(), observed=_obs())
    lines = [r.getMessage() for r in caplog.records
             if "post-reconcile replaced" in r.getMessage()]
    assert lines == [
        f"post-reconcile replaced mac={_A} kind=bmc slot=rabbit-edam/et6b1 by bmc={_B}"]


def test_reconcile_threads_observed_and_evicts_via_2b():
    from flax_classify import post_reconcile as pr
    res = [_res(mac=_A, kind="bmc"), _res(mac=_B, kind="bmc")]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers") as stamp, \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=_facts(macs=(_B,)), now=_now(),
            cfg=_cfg(), observed=_obs())
    assert out == {"deleted": 1, "timers": 0}
    stamp.assert_not_called()
    delete_desired.assert_called_once_with(mock.ANY, owner_role="post", macs=[_A])
    assert {c.kwargs["mac"] for c in upsert_desired.call_args_list} == {_B}


def test_reconcile_2b_and_sticky_purge_same_mac_counted_once():
    from flax_classify import post_reconcile as pr
    res = [_res(mac=_A, kind="bmc"), _res(mac=_B, kind="bmc")]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired"):
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=_facts(macs=(_B,)), now=_now(),
            cfg=_cfg(), derived_macs=frozenset({_B}),
            purged_macs=frozenset({_A}), observed=_obs())
    assert out["deleted"] == 1
    evicted = [m for c in delete_desired.call_args_list for m in c.kwargs["macs"]]
    assert evicted == [_A]


# --- Task 3: duplicate-IPv4 warning (log only)

def test_duplicate_ipv4_helper():
    from flax_classify.post_reconcile import duplicate_ipv4
    rows = [_res(mac=_A, ipv4="172.25.28.103"), _res(mac=_B, ipv4="172.25.28.103"),
            _res(mac=_C, ipv4="172.25.28.3"), _res(mac=_D, ipv4=None),
            _res(mac="ee:ee:ee:ee:ee:ee", ipv4=None)]
    assert duplicate_ipv4(rows, set()) == {"172.25.28.103": rows[:2]}
    assert duplicate_ipv4(rows, {_A}) == {}


def _dup_run(caplog, observed):
    from flax_classify import post_reconcile as pr
    caplog.set_level(logging.WARNING, logger="flax_classify.post_reconcile")
    res = [_res(mac=_A, kind="bmc", ipv4="172.25.28.103"),
           _res(mac=_B, kind="bmc", ipv4="172.25.28.103")]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired"), \
         mock.patch.object(pr, "upsert_desired"):
        pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=_facts(macs=(_B,)), now=_now(),
            cfg=_cfg(), observed=observed)
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "duplicate ipv4" in r.getMessage()]


def test_reconcile_warns_once_per_shared_ipv4(caplog):
    warns = _dup_run(caplog, observed=None)     # no 2b: both rows remain
    assert len(warns) == 1
    msg = warns[0].getMessage()
    assert "172.25.28.103" in msg
    assert f"{_A}@rabbit-edam/et6b1" in msg and f"{_B}@rabbit-edam/et6b1" in msg


def test_reconcile_no_duplicate_warning_once_2b_evicted_the_old_row(caplog):
    assert _dup_run(caplog, observed=_obs()) == []


def test_reconcile_duplicate_check_failure_never_blocks_keepset():
    """The duplicate-ipv4 check's failure (e.g. malformed row) never breaks
    the reconcile -- the loop is guarded, so keep-set desired-emission always
    completes even if duplicate detection raises."""
    from flax_classify import post_reconcile as pr
    facts = _facts(link="link", macs=(_A, _B))
    res = [_res(mac=_A, kind="bmc"), _res(mac=_B, kind="bmc")]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired"), \
         mock.patch.object(pr, "duplicate_ipv4",
                           side_effect=RuntimeError("boom")), \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=facts, now=_now(), cfg=_cfg())
    assert out == {"deleted": 0, "timers": 0}
    # Both retained reservations must have been keep-set-upserted despite the
    # duplicate-check failure.
    upserted = {c.kwargs["mac"] for c in upsert_desired.call_args_list}
    assert upserted == {_A, _B}


# --- final-review fix: 2b must never evict the slot's CURRENT blade ---

def test_2b_never_evicts_host_observe_names_as_current_nic():
    """observe names dd as THIS slot's current host nic (not "departed"), so
    2b must not treat dd's own reservation as the thing bb's arrival
    replaced -- only the departed bmc aa is evicted."""
    res = [_res(mac=_A, kind="bmc"), _res(mac=_B, kind="bmc"),
           _res(mac=_D, kind="host")]
    plan = plan_post_reconcile(
        res, _facts(macs=(_B,)), _now(), _cfg(),
        observed={("rabbit-edam", "et6b1"): {
            "bmc_mac": _B, "nic_mac": _D, "source": "probe_confirmed"}})
    assert plan.deletes == [_A]


def test_reconcile_never_evicts_a_mac_the_reserve_pass_derived():
    """Moved-blade race: kea's row for _A still names this slot, but the
    reserve pass already re-derived _A fresh at another slot this same pass
    (derived_macs). The reconcile's mac-wide delete_desired must not evict
    that fresh row."""
    from flax_classify import post_reconcile as pr
    res = [_res(mac=_A, kind="bmc"), _res(mac=_B, kind="bmc")]
    with mock.patch.object(pr, "read_post_reservations", return_value=res), \
         mock.patch.object(pr, "stamp_post_timers"), \
         mock.patch.object(pr, "delete_desired") as delete_desired, \
         mock.patch.object(pr, "upsert_desired") as upsert_desired:
        out = pr.reconcile_post_reservations(
            pool=mock.MagicMock(), facts=_facts(macs=(_B,)), now=_now(),
            cfg=_cfg(), derived_macs=frozenset({_A}), observed=_obs())
    delete_desired.assert_not_called()
    assert out["deleted"] == 0
    assert _A not in {c.kwargs["mac"] for c in upsert_desired.call_args_list}
