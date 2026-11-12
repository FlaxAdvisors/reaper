"""A PSID change overwrites the OEM image for good, so the station reads it
back with `mstflint ri` before burning (2026-11-04, Dell 020NJD /
DEL2420110034 cards in et25b1/et25b3 at lavergne -- unlocked, so cross-flashed
with no jumper). An unlocked card whose capture fails is not burned."""
import pathlib
import tempfile

from test_unlock_uefi_enable import _burns, _run

_DELL = """Image type:            FS3
FW Version:            14.28.4512
Description:           UID                GuidsNumber
Base GUID:             08c0eb0300a5b6c0        4
Base MAC:              08c0eb17be54            4
PSID:                  DEL2420110034
Security Attributes:   N/A
"""


def _ri(calls):
    return [c for c in calls if c.startswith("mstflint") and " ri " in c]


def test_an_unlocked_oem_card_is_captured_then_burned():
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_DELL,
                          map_psid="DEL2420110034")
        assert _ri(calls) and _burns(calls), got.stdout
        assert calls.index(_ri(calls)[0]) < calls.index(_burns(calls)[0])
        caps = list(pathlib.Path(tmp).rglob("DEL2420110034-14.28.4512-08c0eb17be54.bin"))
        assert len(caps) == 1 and caps[0].read_bytes() == b"card image", got.stdout


def test_a_failed_capture_on_an_unlocked_card_blocks_the_burn():
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_DELL,
                          map_psid="DEL2420110034", capture_fails=True)
        assert not _burns(calls), got.stdout
        assert "burned=failed" in got.stdout and "verdict=problem" in got.stdout, got.stdout


def test_no_capture_when_the_psid_already_matches_the_image():
    """A plain version update keeps the PSID; nothing OEM is lost."""
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp),
                          dev_query=_DELL.replace("DEL2420110034", "MT_2450112034"))
        assert _burns(calls) and not _ri(calls), got.stdout
