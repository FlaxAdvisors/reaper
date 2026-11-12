"""Every card leaves the station with all three boot-ROM settings on.

  EXP_ROM_UEFI_x86_ENABLE  True(1)   UEFI PXE -- the one the unlocked
                                      MT_2450112034 image defaults OFF
  EXP_ROM_PXE_ENABLE       True(1)   the legacy PXE ROM
  LEGACY_BOOT_PROTOCOL     PXE(1)    what that legacy ROM boots

Only the first has been seen wrong (et9b1/et9b4 dumps, 2026-09-22); the other
two default correctly on every card dumped so far. Operator decision the same
day: enforce all three anyway -- one set, one read-back, so an image or OEM
default we have not met cannot ship a card that will not PXE boot.
"""
import pathlib
import tempfile

from test_unlock_rack_signal import run, _FINISHED
from test_unlock_uefi_enable import (
    _CONFIG_QUERY_UEFI_ON,
    _DEV_QUERY_WITH_ORIG_PSID,
    _uefi_sets,
)

_ALL_ON = _CONFIG_QUERY_UEFI_ON
_PXE_OFF = _ALL_ON.replace(
    "EXP_ROM_PXE_ENABLE                          True(1)",
    "EXP_ROM_PXE_ENABLE                          False(0)")
_LEGACY_NONE = _ALL_ON.replace(
    "LEGACY_BOOT_PROTOCOL                        PXE(1)",
    "LEGACY_BOOT_PROTOCOL                        NONE(0)")


def _sets(calls):
    return [c for c in calls if c.startswith("mstconfig") and " set " in c]


def test_a_card_with_every_setting_on_is_not_touched():
    r = run(**_FINISHED)
    assert not _sets(r.calls), r.calls
    assert "verdict=done" in r.result[0], r.why()


def test_the_legacy_pxe_rom_is_turned_back_on():
    r = run(dev_query=_DEV_QUERY_WITH_ORIG_PSID, config_query=_PXE_OFF)
    s = _sets(r.calls)
    assert s and "EXP_ROM_PXE_ENABLE=true" in s[0], r.why()
    assert "EXP_ROM_UEFI_x86_ENABLE" not in s[0], "set a key that was already on"
    assert "verdict=done" in r.result[0] and "romset=PXE" in r.result[0], r.why()


def test_the_legacy_boot_protocol_is_set_to_pxe():
    r = run(dev_query=_DEV_QUERY_WITH_ORIG_PSID, config_query=_LEGACY_NONE)
    s = _sets(r.calls)
    assert s and "LEGACY_BOOT_PROTOCOL=PXE" in s[0], r.why()
    assert "verdict=done" in r.result[0], r.why()


def test_a_uefi_only_fix_records_just_that_key():
    r = run(dev_query=_DEV_QUERY_WITH_ORIG_PSID,
            config_query=_ALL_ON.replace(
                "EXP_ROM_UEFI_x86_ENABLE                     True(1) ",
                "EXP_ROM_UEFI_x86_ENABLE                     False(0)"))
    assert "romset=UEFI" in r.result[0], r.why()
    assert _uefi_sets(r.calls), r.calls


def test_a_setting_that_does_not_stick_is_a_problem():
    r = run(dev_query=_DEV_QUERY_WITH_ORIG_PSID, config_query=_PXE_OFF,
            uefi_set_noop=True)
    assert "verdict=problem" in r.result[0], r.why()


def test_the_result_field_is_bootrom_not_uefi():
    """It covers all three settings; a LEGACY-only fix read `uefi=set` (live
    on et9b1, 2026-09-22) and looked like a UEFI change."""
    r = run(dev_query=_DEV_QUERY_WITH_ORIG_PSID, config_query=_LEGACY_NONE)
    assert " bootrom=set " in r.result[0], r.result[0]
    assert " uefi=" not in r.result[0], r.result[0]
    assert "after UEFI change" not in r.out, r.out
