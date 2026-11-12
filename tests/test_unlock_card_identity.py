"""The station log must identify WHICH card it just handled.

The station's whole output is its log: pass/fail is deliberately not
distinguishable at the rack (unlock_mellanox.sh header), so the log is the only
record that a given card was ever unlocked. Until now it recorded only the PCIe
BDF -- `5e:00.0` -- which names the SLOT, not the card. Every card the station
processes sits in the same slot, so the history was unreadable: reviewing
2026-09-21 we could see four burn attempts but could not tell whether they were
one card retried four times or four different cards. The starting firmware
(14.32.1206 on three runs, 14.27.2606 on a fourth) proved at least two physical
cards, and that was the most the log could support.

mstflint's query already carries the identity -- `Base MAC:` and `Base GUID:`
are in the same output getdevinfo() parses for FW and PSID -- so this costs one
more grep of a string already in hand.

Each card also gets exactly one RESULT line per run, carrying identity plus
outcome, so a history is `grep RESULT /var/log/flax/mezz-flash/*.log` rather
than a reading of prose.
"""
import pathlib
import tempfile

from test_unlock_uefi_enable import (
    _DEV_QUERY_LOCKED,
    _DEV_QUERY_WITH_ORIG_PSID,
    _run,
)

# The identities baked into the two fixtures in test_unlock_uefi_enable.
_UNLOCKED_MAC = "506b4bf9e416"
_UNLOCKED_GUID = "506b4b0300f9e416"
_LOCKED_MAC = "506b4bf9aabb"


def _result_lines(stdout):
    return [ln for ln in stdout.splitlines() if " RESULT " in ln]


def test_the_log_identifies_the_card_by_mac_and_guid():
    """A BDF names the slot; the MAC and GUID name the card in it."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_WITH_ORIG_PSID)
        assert _UNLOCKED_MAC in got.stdout, (
            "log never names the card's Base MAC:\n%s" % got.stdout)
        assert _UNLOCKED_GUID in got.stdout, (
            "log never names the card's Base GUID:\n%s" % got.stdout)


def test_each_card_logs_exactly_one_result_line_carrying_its_mac():
    """One greppable line per card per run -- that IS the history."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_WITH_ORIG_PSID)
        lines = _result_lines(got.stdout)
        assert len(lines) == 1, (
            "expected exactly one RESULT line, got %d:\n%s"
            % (len(lines), got.stdout))
        assert "mac=%s" % _UNLOCKED_MAC in lines[0], lines[0]
        assert "psid=MT_2450112034" in lines[0], lines[0]
        assert "fw=14.32.1912" in lines[0], lines[0]


def test_the_result_line_records_that_the_uefi_rom_was_turned_on():
    """An audit of "did this card get its UEFI ROM" must not need the prose."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_WITH_ORIG_PSID)
        line = _result_lines(got.stdout)[0]
        assert "bootrom=set" in line, (
            "RESULT line does not record the UEFI enable: %s" % line)


def test_the_result_line_records_a_burn_and_the_post_flash_identity():
    """A locked card that gets burned reports burned=yes, under its own MAC."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_LOCKED,
                      map_psid="FB_0000000005", livefish=True)
        line = _result_lines(got.stdout)[0]
        assert "burned=yes" in line, line
        assert "mac=%s" % _LOCKED_MAC in line, line


def test_a_card_that_is_left_alone_still_gets_a_result_line():
    """Silence is not a record. A card with no image in the map is still a card
    that passed through the station, so it must appear in the history."""
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_WITH_ORIG_PSID,
                      map_psid="SOME_OTHER_PSID")
        lines = _result_lines(got.stdout)
        assert len(lines) == 1, (
            "a card with no mapped image left no RESULT line:\n%s" % got.stdout)
        assert "burned=no" in lines[0], lines[0]
        assert "mac=%s" % _UNLOCKED_MAC in lines[0], lines[0]
