"""The station must not skip a card it can still help.

Two gaps closed on 2026-09-21, both cases where the station walked away from a
card that still needed work:

  1. A card at the target FW AND target PSID but STILL `secure-fw`. The
     already-at-target check mirrors post's needsverup(), which compares FW and
     PSID only -- correct there, because post refuses locked cards outright
     (update_mellanox.sh:177) and so never meets one. The station is the only
     place a card can be at the target image and still carry the lock, which is
     exactly what a FAILED unlock looks like: the burn landed, the activation
     did not, and the card came back re-branded but locked. Skipping it means
     the one card that most needs another pass gets none.

  2. A PSID with no FWMAP row. Only three OEM-branded PSIDs are catalogued
     (FB_2450111034, FB_0000000005, HP_2420110034), so any other OEM lock was
     skipped outright. The card still self-reports its part number -- mstconfig
     prints `Name: MCX4411A-ACQ_Ax` -- and the mellanox share carries OPN
     symlinks (MCX4411A-ACQ -> MT_2450112034), so the OPN resolves to an image
     the bundle already carries. Operator decision (2026-09-21): resolve and
     burn, rather than skip and wait for someone to add a map row.
"""
import pathlib
import tempfile

from test_unlock_uefi_enable import _burns, _run

_ID = """Description:           UID                GuidsNumber
Base GUID:             506b4b0300f9e416        4
Base MAC:              506b4bf9e416            4
"""

# At the target image on BOTH counts -- and still locked. A failed unlock.
_AT_TARGET_LOCKED = """Image type:            FS3
FW Version:            14.32.1912
%sPSID:                  MT_2450112034
Security Attributes:   secure-fw
""" % _ID

# The same card with the lock actually cleared: genuinely finished.
_AT_TARGET_UNLOCKED = """Image type:            FS3
FW Version:            14.32.1912
%sPSID:                  MT_2450112034
Security Attributes:   N/A
""" % _ID

# An OEM lock nobody has catalogued.
_UNMAPPED_PSID = """Image type:            FS3
FW Version:            14.27.2606
%sPSID:                  FB_9999999999
Security Attributes:   secure-fw
""" % _ID


# The OPN route needs mstconfig to report the card's Name, which it cannot do
# in livefish -- the only state a LOCKED card can be burned in. So the route
# is exercised here on an unlocked, unmapped card; for an uncatalogued LOCK it
# is currently dead (known gap, 2026-09-22 -- give the lock a FWMAP row).
_UNMAPPED_PSID_UNLOCKED = _UNMAPPED_PSID.replace(
    "Security Attributes:   secure-fw", "Security Attributes:   N/A")


def _result(stdout):
    lines = [ln for ln in stdout.splitlines() if " RESULT " in ln]
    assert lines, "no RESULT line:\n%s" % stdout
    return lines[0]


def test_a_card_at_target_fw_but_still_locked_is_reburned():
    """The failed-unlock case. FW and PSID match the image, so the old check
    said 'already at target' and walked away -- leaving the lock set."""
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_AT_TARGET_LOCKED,
                          livefish=True)
        assert _burns(calls), (
            "a still-locked card was skipped as already-at-target:\n%s"
            % got.stdout)


def test_a_card_at_target_fw_with_the_lock_cleared_is_left_alone():
    """The converse, so the test above is not just 'always burn': a genuinely
    finished card must not be pointlessly re-burned."""
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_AT_TARGET_UNLOCKED)
        assert not _burns(calls), (
            "re-burned a card that was already unlocked: %s" % _burns(calls))
        assert "already at target" in got.stdout, got.stdout


def test_an_unmapped_psid_falls_back_to_the_cards_own_opn():
    """No FWMAP row for FB_9999999999, but the card says it is an
    MCX4411A-ACQ and the share resolves that to MT_2450112034."""
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_UNMAPPED_PSID_UNLOCKED)
        assert _burns(calls), (
            "an unmapped PSID was skipped instead of resolving by OPN:\n%s"
            % got.stdout)


def test_the_result_line_records_which_opn_was_used():
    """An OPN-routed burn is a decision made without a map row, so the log has
    to say what it decided on."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), dev_query=_UNMAPPED_PSID_UNLOCKED)
        assert "opn=MCX4411A-ACQ" in _result(got.stdout), _result(got.stdout)


def test_an_unmapped_psid_with_an_unresolvable_opn_is_not_burned():
    """The floor. With neither a PSID row nor an OPN the share knows, there is
    no image to choose -- do not guess."""
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_UNMAPPED_PSID,
                          opn_map="MCX9999X-ZZZ\tMT_0000000000\n")
        assert not _burns(calls), (
            "burned on an OPN the share does not know: %s" % _burns(calls))


def test_an_unresolvable_card_still_gets_its_uefi_pass_and_a_result_line():
    """Not burnable is not the same as not worth touching."""
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_UNMAPPED_PSID_UNLOCKED,
                          opn_map="MCX9999X-ZZZ\tMT_0000000000\n")
        from test_unlock_uefi_enable import _uefi_sets
        assert _uefi_sets(calls), "no UEFI pass on an unburnable card"
        assert "burned=no" in _result(got.stdout), _result(got.stdout)
