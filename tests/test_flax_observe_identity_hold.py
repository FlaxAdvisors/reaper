"""Part 2 / B: flax_observe.identity_hold (pure decision + probe helpers)."""
from flax_observe import identity_hold as ih

OLD = "b8:59:9f:dd:ea:2a"
NEW = "b8:59:9f:dd:ea:2b"


def _d(**kw):
    base = dict(latched_mac=OLD, chassis_sn="SN1", new_mac=None, hold_age=0.0,
                seen_elsewhere=False, serial_of_new=None)
    base.update(kw)
    return ih.decide(**base)


def test_constants():
    assert ih.HOLD_SECS == 900 and ih.PROBE_RETRY_SECS == 60


def test_no_identity_is_live():
    assert _d(latched_mac=None) == ih.LIVE
    assert _d(chassis_sn=None) == ih.LIVE
    assert _d(chassis_sn=None, new_mac=NEW) == ih.LIVE


def test_same_mac_is_live():
    assert _d(new_mac=OLD) == ih.LIVE


def test_empty_fdb_holds_then_expires():
    assert _d(new_mac=None, hold_age=0) == ih.HELD
    assert _d(new_mac=None, hold_age=899.9) == ih.HELD
    assert _d(new_mac=None, hold_age=900) == ih.EXPIRED


def test_dup_mac_holds_without_probe():
    """Review Focus 3: a MAC also on another port is ignored (held), even if
    a serial were somehow known."""
    assert _d(new_mac=NEW, seen_elsewhere=True, serial_of_new="SN1") == ih.HELD
    assert _d(new_mac=NEW, seen_elsewhere=True, hold_age=900) == ih.EXPIRED


def test_same_serial_is_flip():
    assert _d(new_mac=NEW, serial_of_new="SN1") == ih.FLIPPED
    assert _d(new_mac=NEW, serial_of_new=" SN1 ") == ih.FLIPPED


def test_different_serial_is_swap():
    assert _d(new_mac=NEW, serial_of_new="SN2") == ih.SWAPPED


def test_unreadable_bmc_holds_then_swaps():
    """A confirmed BMC kind whose serial cannot be read: bounded by the hold."""
    assert _d(new_mac=NEW, new_is_bmc=True, hold_age=10) == ih.HELD
    assert _d(new_mac=NEW, new_is_bmc=True, hold_age=900) == ih.SWAPPED


def test_a_mac_that_is_not_a_bmc_never_swaps():
    """lavergne 2026-10-28: six Leopard BMCs went mute for 25+ min while their
    hosts ran inventory; the lone host NIC was the single-MAC "BMC" and the
    900 s expiry swapped it in, handing the host the BMC's reservation. A MAC
    that never probed as a BMC is not evidence of a different blade (a real
    pull is the committed link-down's job)."""
    for age in (0, 899.9, 900, 3600, 86400):
        assert _d(new_mac=NEW, new_is_bmc=False, hold_age=age) == ih.HELD


def test_new_is_bmc_defaults_to_not_a_bmc():
    assert _d(new_mac=NEW, hold_age=900) == ih.HELD


# ── memo_serial ────────────────────────────────────────────────────────────

def test_memo_probes_once_per_mac_when_readable():
    calls = []
    reader = lambda mac: calls.append(mac) or "SN1"
    ps = {}
    assert ih.memo_serial(ps, NEW, reader, now_iso="T0", secs_since=lambda t: 0) == "SN1"
    assert ih.memo_serial(ps, NEW, reader, now_iso="T1", secs_since=lambda t: 999) == "SN1"
    assert calls == [NEW]


def test_memo_retries_unreadable_every_60s():
    calls = []
    reader = lambda mac: calls.append(mac) or None
    ps = {}
    ih.memo_serial(ps, NEW, reader, now_iso="T0", secs_since=lambda t: 0)
    ih.memo_serial(ps, NEW, reader, now_iso="T1", secs_since=lambda t: 59)
    assert calls == [NEW]
    ih.memo_serial(ps, NEW, reader, now_iso="T2", secs_since=lambda t: 60)
    assert calls == [NEW, NEW]


def test_memo_new_mac_probes_again():
    calls = []
    reader = lambda mac: calls.append(mac) or "SN9"
    ps = {"identity_probe": {"mac": OLD, "at": "T0", "serial": "SN1"}}
    assert ih.memo_serial(ps, NEW, reader, now_iso="T1", secs_since=lambda t: 0) == "SN9"
    assert calls == [NEW]
    assert ps["identity_probe"] == {"mac": NEW, "at": "T1", "serial": "SN9"}


# ── read_serial_for_mac ────────────────────────────────────────────────────

def _read(**kw):
    base = dict(
        reach=lambda mac: ("fe80::1%eth1.17", True),
        cached_probe=None,
        probe_kind=lambda target: {"kind": "openbmc", "vendor": "phosphor",
                                   "creds_used": ("root", "x")},
        serial_via_ssh=lambda vendor, kind: True,
        serial_openbmc=lambda target, creds: ("SN1", "ok"),
        serial_traditional=lambda target, creds: ("SNT", "ok"),
        serial_read=lambda r: r,
        bmc_creds=[{"bmcuser": "u", "bmcpass": "p"}],
        confirmed_kinds=frozenset({"openbmc", "traditional", "redfish"}),
    )
    base.update(kw)
    return ih.read_serial_for_mac(NEW, **base)


def test_reads_ssh_serial_over_link_local():
    assert _read() == "SN1"


def test_unreachable_is_none():
    assert _read(reach=lambda mac: (None, False)) is None


def test_not_a_confirmed_bmc_is_none():
    """Review Focus 1: a lone host NIC MAC is not a BMC -> no serial, no probe
    of a serial reader at all."""
    called = []
    assert _read(probe_kind=lambda t: {"kind": "unknown", "creds_used": None},
                 serial_openbmc=lambda *a: called.append(1)) is None
    assert called == []


def test_cached_probe_is_reused():
    called = []
    assert _read(cached_probe={"kind": "openbmc", "vendor": "phosphor",
                               "creds_used": ("root", "x")},
                 probe_kind=lambda t: called.append(t)) == "SN1"
    assert called == []


def test_traditional_needs_ipv4_not_link_local():
    assert _read(serial_via_ssh=lambda v, k: False) is None
    assert _read(serial_via_ssh=lambda v, k: False,
                 reach=lambda mac: ("172.17.0.50", False)) == "SNT"


def test_ssh_absent_falls_back_to_lan_walk_on_ipv4():
    assert _read(reach=lambda mac: ("172.17.0.50", False),
                 serial_openbmc=lambda t, c: (None, "absent"),
                 serial_traditional=lambda t, c: ("SNL", "ok")) == "SNL"


def test_no_serial_state_is_none():
    assert _read(serial_openbmc=lambda t, c: (None, "no_serial")) is None


# --- pair rule (2026-11-06): a complete new pair with the old pair gone ------
def test_complete_new_pair_with_old_gone_swaps_before_serial():
    assert _d(new_mac=NEW, latched_gone=True, new_pair_complete=True,
              serial_of_new=None) == ih.SWAPPED


def test_complete_new_pair_but_old_still_present_holds():
    assert _d(new_mac=NEW, latched_gone=False, new_pair_complete=True,
              serial_of_new=None) == ih.HELD


def test_lone_new_mac_with_old_gone_still_holds():
    assert _d(new_mac=NEW, latched_gone=True, new_pair_complete=False,
              serial_of_new=None) == ih.HELD


def test_pair_rule_does_not_override_seen_elsewhere():
    assert _d(new_mac=NEW, seen_elsewhere=True, latched_gone=True,
              new_pair_complete=True) == ih.HELD


def test_pair_rule_yields_to_a_readable_same_serial():
    """Final review: a readable new BMC reporting the latched serial is the same
    chassis (moved mezz card) -> FLIPPED, even with a complete new pair."""
    assert _d(new_mac=NEW, latched_gone=True, new_pair_complete=True,
              serial_of_new="SN1") == ih.FLIPPED


def test_pair_rule_with_readable_different_serial_still_swaps():
    assert _d(new_mac=NEW, latched_gone=True, new_pair_complete=True,
              serial_of_new="SN2") == ih.SWAPPED
