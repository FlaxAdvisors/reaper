"""The station powers the node off only when it actually changed the NIC.

A node carrying an already-finished card (target FW+PSID, lock gone, UEFI ROM
on) used to be shut down on EVERY boot anyway: unlock_mellanox.sh ended with an
unconditional `shutdown -h now` gated only by SHUTDOWN_ON_DONE. With the unit
enabled, such a node could never be booted to investigate anything -- et9b1 on
2026-09-22 came up in u24, ran a no-op pass, lit IDENT and went dark.

Operator decision 2026-09-22: power off only if the run burned a card or set
its UEFI ROM. A run that changed nothing leaves the node up. IDENT still
blinks, because it means "station finished", which is still true.
"""
import os
import pathlib
import subprocess
import tempfile

from test_unlock_uefi_enable import (
    _CONFIG_QUERY_UEFI_OFF,
    _CONFIG_QUERY_UEFI_ON,
    _DEV_QUERY_LOCKED,
    _DEV_QUERY_WITH_ORIG_PSID,
    _bundle,
)


def _run(tmp_path, **kw):
    """Like test_unlock_uefi_enable._run, but with SHUTDOWN_ON_DONE=1 -- the
    deployed default -- so the fake `shutdown` records whether it was asked."""
    d, bindir = _bundle(tmp_path, **kw)
    env = dict(
        os.environ,
        PATH="%s:%s" % (bindir, os.environ["PATH"]),
        FAKEDIR=str(d),
        MEZZ_FLASH_ALL="1",
        SHUTDOWN_ON_DONE="1",
        MEZZ_SOL_DEV=str(d / "sol.out"),
        MEZZ_ISSUE=str(d / "mezz-flash.issue"),
    )
    got = subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                         capture_output=True, text=True, env=env, cwd=str(d))
    calls = (d / "calls.log").read_text().splitlines()
    return got, calls


def _shutdowns(calls):
    return [c for c in calls if c.startswith("shutdown")]


def _identify(calls):
    return [c for c in calls if c.startswith("ipmitool") and "identify" in c]


def test_a_finished_card_leaves_the_node_up():
    """Target FW+PSID, unlocked, UEFI already on: nothing to do, so no power-off."""
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp),
                          dev_query=_DEV_QUERY_WITH_ORIG_PSID,
                          config_query=_CONFIG_QUERY_UEFI_ON)
        assert not _shutdowns(calls), (
            "powered off a node whose card needed nothing.\ncalls:\n%s\n\n"
            "stdout:\n%s" % ("\n".join(calls), got.stdout))
        assert "NO-CHANGE" in got.stdout, got.stdout
        assert _identify(calls), "IDENT must still mean 'station finished'"


def test_setting_the_uefi_rom_still_powers_off():
    """Unlocked card at target FW, but ROM off: the station changed the card."""
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp),
                          dev_query=_DEV_QUERY_WITH_ORIG_PSID,
                          config_query=_CONFIG_QUERY_UEFI_OFF)
        assert _shutdowns(calls), (
            "set the UEFI ROM but did not power off.\nstdout:\n%s" % got.stdout)


def test_burning_a_locked_card_still_powers_off():
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_LOCKED,
                          map_psid="FB_0000000005",
                          config_query=_CONFIG_QUERY_UEFI_ON)
        assert _shutdowns(calls), (
            "burned a card but did not power off.\nstdout:\n%s" % got.stdout)


def test_shutdown_on_done_0_still_wins_after_a_change():
    """The dev override keeps working: a change plus SHUTDOWN_ON_DONE=0 stays up."""
    with tempfile.TemporaryDirectory() as tmp:
        d, bindir = _bundle(pathlib.Path(tmp), dev_query=_DEV_QUERY_LOCKED,
                            map_psid="FB_0000000005")
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   FAKEDIR=str(d), MEZZ_FLASH_ALL="1", SHUTDOWN_ON_DONE="0",
                   MEZZ_SOL_DEV=str(d / "sol.out"),
                   MEZZ_ISSUE=str(d / "mezz-flash.issue"))
        subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                       capture_output=True, text=True, env=env, cwd=str(d))
        calls = (d / "calls.log").read_text().splitlines()
        assert not _shutdowns(calls), calls
