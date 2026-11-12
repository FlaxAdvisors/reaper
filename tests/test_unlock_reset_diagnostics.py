"""Capture enough data on the next locked card to settle the one-pass question.

The post lane finishes a card in a single boot: burn, mstfwreset, sleep 5, then
the UEFI tweak (update_mellanox.sh flashnicfw + the loop's `if needuefi; then
setuefi`). No reboot anywhere. The station cannot currently do that -- on
2026-09-21 every burn ended

    -W- Failed to update FW boot address. Power cycle the device in order to
        load the new FW.
    -E- Failed to send Register MFRL: ME_MAD_SEND_FAILED (8).

-- so the card needed a cold power cycle and a second pass.

post never meets this because it refuses locked cards outright
(update_mellanox.sh:177, `devsecure == devuntouchable` where devuntouchable is
"secure-fw"). The station is the only place we burn one, so it is the only
place the question arises.

What we do NOT know is whether that is inherent to a just-burned locked card or
something about how the station asks. These tests pin the logging needed to
find out on the next real card, WITHOUT changing what the station does to it:

  - the reset levels the card reports BEFORE the burn (a locked baseline we
    have never captured -- the only reading we have is from an already-unlocked
    card, which reports 3 and 4 supported)
  - the reset levels it reports AFTER a failed reset: if everything comes back
    "Not Supported", a power cycle is inherent and no reset-level change helps
  - whether the BURN updated the FW boot address, which is the actual failure.
    mstflint reports it before mstfwreset is ever called, so the boot address
    -- not the reset -- is what decides whether one pass is possible.
  - mstfwreset's exit status, as a number, rather than prose in the log

Read the answer off the next locked card's RESULT line.
"""
import pathlib
import tempfile

from test_unlock_uefi_enable import _DEV_QUERY_LOCKED, _run

# livefish: a locked card is only ever burned with the jumper fitted.
_LOCKED = dict(dev_query=_DEV_QUERY_LOCKED, map_psid="FB_0000000005",
               livefish=True)


def _result(stdout):
    lines = [ln for ln in stdout.splitlines() if " RESULT " in ln]
    assert lines, "no RESULT line at all:\n%s" % stdout
    return lines[0]


def test_the_supported_reset_levels_are_captured_before_the_burn():
    """The locked baseline we have never had. Without it, a post-failure
    reading proves nothing -- we could not tell a card that lost the
    capability from one that never advertised it."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), fwreset_rc=0, **_LOCKED)
        assert "reset-levels (pre-burn)" in got.stdout.lower(), (
            "no pre-burn reset-level capture:\n%s" % got.stdout)
        assert "Warm Reboot" in got.stdout, (
            "the capture did not include mstfwreset's actual output:\n%s"
            % got.stdout)


def test_a_failed_reset_captures_the_levels_the_card_still_reports():
    """The decisive reading: if a just-burned locked card reports nothing
    supported, a cold power cycle is inherent and no --level change helps."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), fwreset_rc=1, **_LOCKED)
        assert "reset-levels (post-failure)" in got.stdout.lower(), (
            "a failed reset captured no diagnostic:\n%s" % got.stdout)


def test_no_post_failure_capture_when_the_reset_succeeded():
    """Only pay for the diagnostic when there is something to diagnose."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), fwreset_rc=0, **_LOCKED)
        assert "reset-levels (post-failure)" not in got.stdout.lower(), (
            "captured a post-failure diagnostic on a clean reset:\n%s"
            % got.stdout)


def test_the_result_line_records_the_reset_exit_status():
    """A number, not prose buried in mstfwreset's output."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), fwreset_rc=1, **_LOCKED)
        assert "resetrc=1" in _result(got.stdout), _result(got.stdout)


def test_the_result_line_records_a_failed_boot_address_update():
    """THE signal for the one-pass question. mstflint reports this during the
    BURN, before mstfwreset runs, so it -- not the reset -- is what decides
    whether a single pass can ever work."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), fwreset_rc=1, bootaddr_fail=True,
                      **_LOCKED)
        assert "bootaddr=failed" in _result(got.stdout), _result(got.stdout)


def test_the_result_line_records_a_good_boot_address_update():
    """The converse, so the assertion above is not just always-true: a burn
    that did update the boot address must say so. That combination -- boot
    address OK -- is what a working single pass looks like."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), fwreset_rc=0, bootaddr_fail=False,
                      **_LOCKED)
        assert "bootaddr=ok" in _result(got.stdout), _result(got.stdout)


def test_a_card_that_was_not_burned_reports_no_boot_address_verdict():
    """An already-unlocked card is not evidence either way, so it must not
    manufacture a bootaddr reading."""
    with tempfile.TemporaryDirectory() as tmp:
        from test_unlock_uefi_enable import _DEV_QUERY_WITH_ORIG_PSID
        got, _ = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_WITH_ORIG_PSID)
        line = _result(got.stdout)
        assert "bootaddr=n/a" in line, line
        assert "burned=no" in line, line
