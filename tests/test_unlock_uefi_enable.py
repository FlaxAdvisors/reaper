"""The station must always leave a card with the UEFI expansion ROM enabled.

A card that comes off the station with EXP_ROM_UEFI_x86_ENABLE=False(0) has a
working PXE ROM but no UEFI one, so it boots legacy PXE and nothing else. The
fleet standard is UEFI on -- update_mellanox.sh has always forced it, and every
card that went through the post lane reports True(1).

Regression for et26b3 (2026-09-21). Its mezzanine card was unlocked correctly
(14.32.1912 / MT_2450112034 / Security Attributes: N/A) and still came out with
EXP_ROM_UEFI_x86_ENABLE=False(0), while the PCIe uplink in the same chassis read
True(1). Two independent holes in unlock_mellanox.sh let that through:

  1. getdevinfo() greps an UNANCHORED 'PSID:', which also matches the
     'Orig PSID:' line mstflint prints for a card whose PSID was changed. That
     makes $devpsid two whitespace-separated tokens, the FWMAP lookup misses,
     and `continue` jumps past the UEFI block that sits after it in the loop.
     Both re-runs that would otherwise have healed the card (08:03, 08:32) died
     exactly here, logging

         5e:00.0: PSID MT_2450112034
         FB_0000000005 is not in the map; skipping.

  2. An unreadable mstconfig query leaves $uefival empty, and `${uefival:-1}`
     reads that as "already enabled" -- so the one case where we cannot prove
     the ROM is on is the case we skip. The earlier runs (06:13-07:41) took
     this path: mstfwreset had failed with ME_MAD_SEND_FAILED(8), so the card
     was still running the outgoing locked image when its config was read.

common_mellanox.sh is shared VERBATIM with the post lane and must not be edited
to suit the station (see the header of unlock_mellanox.sh and
tests/test_unlock_shell_compat.py), so both fixes belong in the station script.
"""
import os
import shutil
import subprocess

_REAPER = os.path.join(os.path.dirname(__file__), os.pardir)
_PAYLOAD = os.path.join(_REAPER, "scripts", "unlock-with-ubuntu", "payload")
_STATION = os.path.join(_PAYLOAD, "unlock_mellanox.sh")
_COMMON = os.path.join(_REAPER, "roles", "apply_pxe_payloads", "files", "post",
                       "common_mellanox.sh")

_DEV = "5e:00.0"
_PSID = "MT_2450112034"

# mstflint's device query for a card that has ALREADY been unlocked: it now
# reports the new PSID plus an 'Orig PSID:' record of the one it replaced.
_DEV_QUERY_WITH_ORIG_PSID = """Image type:            FS3
FW Version:            14.32.1912
FW Release Date:       21.1.2026
Rom Info:              type=UEFI version=14.25.17 cpu=AMD64,AARCH64
                       type=PXE version=3.6.502 cpu=AMD64
Description:           UID                GuidsNumber
Base GUID:             506b4b0300f9e416        4
Base MAC:              506b4bf9e416            4
PSID:                  MT_2450112034
Orig PSID:             FB_0000000005
Security Attributes:   N/A
"""

_IMG_QUERY = """Image type:            FS3
FW Version:            14.32.1912
PSID:                  MT_2450112034
"""

# Trailing padding is how real mstconfig prints; the script's sed allows it.
_CONFIG_QUERY_UEFI_OFF = """Device type:        ConnectX4LX
Name:               MCX4411A-ACQ_Ax
Configurations:                                     Next Boot
        LEGACY_BOOT_PROTOCOL                        PXE(1)
        EXP_ROM_UEFI_ARM_ENABLE                     False(0)
        EXP_ROM_UEFI_x86_ENABLE                     False(0)
        EXP_ROM_PXE_ENABLE                          True(1)
"""

_FAKE_MST = r"""#!/bin/bash
# One fake standing in for mstflint/mstconfig/mstfwreset. Records every call,
# then answers from canned files so a test can pick the card's state.
echo "$(basename "$0") $*" >> "$FAKEDIR/calls.log"
me=$(basename "$0")
case "$me" in
mstflint)
    # `ri <file>`: read the card's image back. capture_fails = a read that dies.
    for a in "$@"; do [ "$a" = "ri" ] && {
        [ -f "$FAKEDIR/capture_fails" ] && { echo "-E- Failed to read image"; exit 1; }
        printf 'card image' > "${@: -1}"
        exit 0
    }; done
    for a in "$@"; do [ "$a" = "burn" ] && {
        if [ -f "$FAKEDIR/burn_refused" ]; then
            echo "-E- Burning FS3 image failed: Changing PSID is unsupported under controlled FW. You can try to run again with the flag \"--no_fw_ctrl\"."
            exit 1
        fi
        echo "Burning FW image without signatures - OK"
        if [ -f "$FAKEDIR/burn_bootaddr_fail" ]; then
            echo "-W- Failed to update FW boot address. Power cycle the device in order to load the new FW."
        fi
        echo "Restoring signature                     - OK"
        echo "-I- To load new FW run reboot machine."
        exit 0
    }; done
    case "$*" in
        *-i*) cat "$FAKEDIR/img_query.txt" ;;
        *)    cat "$FAKEDIR/dev_query.txt" ;;
    esac
    ;;
mstconfig)
    # Jumper fitted: the card is in livefish and mstconfig can do nothing.
    if [ -f "$FAKEDIR/livefish" ]; then
        echo "-E- Device in Livefish mode is not supported"
        exit 1
    fi
    for a in "$@"; do [ "$a" = "set" ] && {
        if [ -f "$FAKEDIR/uefi_set_fails" ]; then
            echo "-E- Failed to set configuration"
            exit 1
        fi
        # A real set lands in the Next Boot column the next query reads,
        # unless the test wants a set that silently does not stick.
        if [ ! -f "$FAKEDIR/uefi_set_noop" ]; then
            for kv in "$@"; do
                case "$kv" in
                EXP_ROM_*=true) k=${kv%%=*}
                    sed -i "s/\($k *\)False(0)/\1True(1) /" "$FAKEDIR/config_query.txt" ;;
                LEGACY_BOOT_PROTOCOL=PXE)
                    sed -i "s/\(LEGACY_BOOT_PROTOCOL *\)[A-Z]*([0-9])/\1PXE(1)/" "$FAKEDIR/config_query.txt" ;;
                esac
            done
        fi
        exit 0
    }; done
    cat "$FAKEDIR/config_query.txt"
    exit "$(cat "$FAKEDIR/config_query_rc.txt" 2>/dev/null || echo 0)"
    ;;
mstfwreset)
    for a in "$@"; do [ "$a" = "query" ] && { cat "$FAKEDIR/reset_query.txt"; exit 0; }; done
    rc=$(cat "$FAKEDIR/fwreset_rc.txt" 2>/dev/null || echo 0)
    if [ "$rc" != "0" ]; then
        echo "-E- Failed to send Register MFRL: ME_MAD_SEND_FAILED (8)."
    fi
    exit "$rc"
    ;;
esac
exit 0
"""

_FAKE_TRIVIAL = """#!/bin/bash
echo "$(basename "$0") $*" >> "$FAKEDIR/calls.log"
exit 0
"""

_FAKE_LSPCI = """#!/bin/bash
[ -f "$FAKEDIR/nocards" ] && exit 0
echo "%s Ethernet controller: Mellanox Technologies MT27710 Family [ConnectX-4 Lx]"
""" % _DEV

# Stands in for the real curl push. Logs every invocation like every other
# double in this harness -- a test counting pushes needs something to count,
# or the assertion can never fail. Exits 7 (curl's own "couldn't connect")
# when curl_fails is armed, so pushstatus() sees exactly what a dead link
# looks like.
_FAKE_CURL = """#!/bin/bash
echo "$(basename "$0") $*" >> "$FAKEDIR/calls.log"
[ -f "$FAKEDIR/curl_fails" ] && exit 7
exit 0
"""


_RESET_QUERY = """
Reset-levels:
0: Driver, PCI link, network link will remain up ("live-Patch")  -Not Supported
3: Driver restart and PCI reset                                  -Supported     (default)
4: Warm Reboot                                                   -Supported
"""


def _bundle(tmp_path, dev_query, config_query=_CONFIG_QUERY_UEFI_OFF,
            config_query_rc=0, fwreset_rc=0, map_psid=_PSID,
            bootaddr_fail=False, reset_query=_RESET_QUERY,
            opn_map="MCX4411A-ACQ\tMT_2450112034\n", livefish=False,
            burn_refused=False, uefi_set_fails=False, uefi_set_noop=False,
            no_ident=False, curl_fails=False, capture_fails=False):
    """A runnable station bundle in tmp_path, with the REAL scripts in it."""
    d = tmp_path / "mezzflash"
    d.mkdir()
    shutil.copy(_STATION, d / "unlock_mellanox.sh")
    shutil.copy(_COMMON, d / "common_mellanox.sh")
    (d / "unlock_mellanox.sh").chmod(0o755)

    # station_ident.py: a stub standing in for the real in-band FRU read, so
    # every bundle answers "who is this blade" without touching ipmitool.
    # Logs its own invocation to calls.log, like every other double in this
    # harness (mstflint, mstconfig, mstfwreset, ipmitool, lspci, agetty,
    # shutdown, sleep) -- a test asserting it was called at most once needs
    # something to count, or the assertion can never fail.
    _log_call = 'echo "$(basename "$0") $*" >> "$FAKEDIR/calls.log"\n'
    if no_ident:
        ident_body = ('#!/bin/sh\n' + _log_call +
                      'echo \'{"station_sn":"","serial_field":"Chassis Serial",'
                      '"family":"leopard","state":"no_serial","bmc_mac":"","bmc_ip":"",'
                      '"host_mac":"","host":"leopard9b1"}\'\nexit 1\n')
    else:
        ident_body = ('#!/bin/sh\n' + _log_call +
                      'echo \'{"station_sn":"QCFF06191403C6",'
                      '"serial_field":"Chassis Serial","family":"leopard","state":"ok",'
                      '"bmc_mac":"50:6b:4b:da:9d:ce","bmc_ip":"172.16.9.101",'
                      '"host_mac":"50:6b:4b:da:9d:cc","host":"leopard9b1"}\'\nexit 0\n')
    (d / "station_ident.py").write_text(ident_body)
    (d / "station_ident.py").chmod(0o755)

    (d / "MANIFEST").write_text("bundle : test\n")
    (d / "dev_query.txt").write_text(dev_query)
    (d / "img_query.txt").write_text(_IMG_QUERY)
    (d / "config_query.txt").write_text(config_query)
    (d / "config_query_rc.txt").write_text(str(config_query_rc))
    (d / "fwreset_rc.txt").write_text(str(fwreset_rc))
    (d / "reset_query.txt").write_text(reset_query)
    for flag, on in (("burn_bootaddr_fail", bootaddr_fail),
                     ("livefish", livefish),
                     ("burn_refused", burn_refused),
                     ("uefi_set_fails", uefi_set_fails),
                     ("uefi_set_noop", uefi_set_noop),
                     ("curl_fails", curl_fails),
                     ("capture_fails", capture_fails)):
        if on:
            (d / flag).write_text("")
    (d / "calls.log").write_text("")

    fwdir = d / "fw" / _PSID
    fwdir.mkdir(parents=True)
    img = fwdir / "fw-test.bin"
    img.write_bytes(b"not a real firmware image")
    rel = "fw/%s/fw-test.bin" % _PSID
    digest = subprocess.run(["sha256sum", str(img)], capture_output=True,
                            text=True).stdout.split()[0]
    (d / "SHA256SUMS").write_text("%s  %s\n" % (digest, rel))
    # Keyed by the PSID the CARD reports; the image dir is named separately.
    (d / "nic_fw_map.tsv").write_text("%s\t%s\tfw-test.bin\n" % (map_psid, _PSID))
    # OPN -> PSID, built from the share's symlinks at bundle time.
    (d / "nic_opn_map.tsv").write_text(opn_map)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("mstflint", "mstconfig", "mstfwreset"):
        f = bindir / name
        f.write_text(_FAKE_MST)
        f.chmod(0o755)
    # `sleep` is stubbed out: the script's settle waits are real on a station
    # and pure dead time here.
    for name, body in (("ipmitool", _FAKE_TRIVIAL), ("lspci", _FAKE_LSPCI),
                       ("agetty", _FAKE_TRIVIAL),
                       ("shutdown", _FAKE_TRIVIAL), ("sleep", _FAKE_TRIVIAL),
                       ("curl", _FAKE_CURL)):
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    return d, bindir


def _run(tmp_path, **kw):
    """Run the station end to end. -> (CompletedProcess, [calls])"""
    d, bindir = _bundle(tmp_path, **kw)
    env = dict(
        os.environ,
        PATH="%s:%s" % (bindir, os.environ["PATH"]),
        FAKEDIR=str(d),
        MEZZ_FLASH_ALL="1",      # bypass mezz_select; lspci is faked
        SHUTDOWN_ON_DONE="0",
        # The rack banner goes to the SOL tty and /etc/issue.d on a station;
        # keep a test run off this host's real ones.
        MEZZ_SOL_DEV=str(d / "sol.out"),
        MEZZ_ISSUE=str(d / "mezz-flash.issue"),
        MEZZ_LOGDIR=str(d / "logs"),
    )
    got = subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                         capture_output=True, text=True, env=env, cwd=str(d))
    calls = (d / "calls.log").read_text().splitlines()
    return got, calls


def _uefi_sets(calls):
    return [c for c in calls
            if c.startswith("mstconfig") and " set " in c
            and "EXP_ROM_UEFI_x86_ENABLE=true" in c]


def _burns(calls):
    return [c for c in calls if c.startswith("mstflint") and c.endswith(" burn")]


def test_uefi_is_enabled_when_mstflint_also_reports_an_orig_psid():
    """The et26b3 hole: a second PSID-bearing line must not cost the UEFI pass.

    Fails today because $devpsid becomes "MT_2450112034\\nFB_0000000005", the
    FWMAP lookup misses and `continue` skips the UEFI block entirely.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        import pathlib
        got, calls = _run(pathlib.Path(tmp),
                          dev_query=_DEV_QUERY_WITH_ORIG_PSID)
        assert _uefi_sets(calls), (
            "station never enabled the UEFI expansion ROM.\ncalls:\n%s\n\n"
            "stdout:\n%s" % ("\n".join(calls), got.stdout))


def test_no_burn_when_already_at_target_despite_an_orig_psid_line():
    """The PSID must be read as ONE value, so an already-unlocked card is
    recognised as already-at-target and is not needlessly re-burned.

    Fails today: the polluted $devpsid misses the map, so the script reports
    "is not in the map; skipping" instead of "already at target FW+PSID".
    """
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp),
                          dev_query=_DEV_QUERY_WITH_ORIG_PSID)
        assert not _burns(calls), (
            "re-burned a card already at target FW+PSID: %s" % _burns(calls))
        assert "is not in the map" not in got.stdout, (
            "PSID was not read as a single value:\n%s" % got.stdout)


def test_uefi_is_enabled_when_the_config_query_cannot_be_read():
    """Fail CLOSED: if we cannot prove the ROM is on, set it.

    Fails today: an empty $uefival hits `${uefival:-1}` and is read as
    already-enabled, which is precisely backwards -- an unreadable config is
    the case with the least evidence, not the most.
    """
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp),
                          dev_query=_DEV_QUERY_WITH_ORIG_PSID,
                          config_query="-E- Failed to query device\n",
                          config_query_rc=1)
        assert _uefi_sets(calls), (
            "an unreadable config query silently skipped the UEFI enable.\n"
            "calls:\n%s" % "\n".join(calls))


# A card as it arrives at the station: still carrying the OEM lock, one PSID.
_DEV_QUERY_LOCKED = """Image type:            FS3
FW Version:            14.27.2606
Description:           UID                GuidsNumber
Base GUID:             506b4b0300f9aabb        4
Base MAC:              506b4bf9aabb            4
PSID:                  FB_0000000005
Security Attributes:   secure-fw
"""


def test_a_jumpered_locked_card_is_burned_and_its_uefi_rom_deferred():
    """The station's main line. REVISED 2026-09-22: this used to assert the
    UEFI set ran in the same pass as the burn. Live data says it cannot -- a
    card is only burnable with the jumper fitted (livefish), and livefish
    refuses every mstconfig write. The UEFI ROM is the NEXT, jumper-less pass
    (tests/test_unlock_rack_signal.py)."""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        # The map is keyed by the PSID the card reports NOW, so point the
        # locked card's PSID at the bundle image.
        got, calls = _run(d, dev_query=_DEV_QUERY_LOCKED, map_psid="FB_0000000005",
                          livefish=True)
        assert _burns(calls), (
            "a locked card was not burned.\ncalls:\n%s\n\nstdout:\n%s"
            % ("\n".join(calls), got.stdout))
        assert not _uefi_sets(calls), (
            "tried a UEFI set in livefish.\ncalls:\n%s" % "\n".join(calls))


# The same config listing, but with the ROM already reported ON. On a card
# whose new image is in flash but NOT running, this is the OUTGOING image
# talking -- it says nothing about what the card will boot.
_CONFIG_QUERY_UEFI_ON = _CONFIG_QUERY_UEFI_OFF.replace(
    "EXP_ROM_UEFI_x86_ENABLE                     False(0)",
    "EXP_ROM_UEFI_x86_ENABLE                     True(1) ")


# Unlocked, but on an older image: burnable WITHOUT the jumper, so the
# activate-then-UEFI path is real for it (a locked card never gets there now).
_DEV_QUERY_UNLOCKED_OLD = """Image type:            FS3
FW Version:            14.27.2606
Description:           UID                GuidsNumber
Base GUID:             506b4b0300f9aabb        4
Base MAC:              506b4bf9aabb            4
PSID:                  MT_2450112034
Security Attributes:   N/A
"""


def test_a_failed_activation_is_reported_instead_of_swallowed():
    """mstfwreset failing means the new FW is in flash but not running. The old
    code ignored its exit status entirely; the log must say so, and must say
    the card needs another pass."""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        got, _ = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_LOCKED,
                      map_psid="FB_0000000005", fwreset_rc=1, livefish=True)
        result = [ln for ln in got.stdout.splitlines() if " RESULT " in ln]
        assert result and "activated=no" in result[0], (
            "a failed mstfwreset was not recorded:\n%s" % got.stdout)
        assert "NEEDS-SECOND-PASS" in got.stdout, (
            "log does not tell the operator the card is unfinished:\n%s"
            % got.stdout)


def test_a_card_that_did_not_activate_still_gets_the_uefi_rom_set():
    """The stale config says the ROM is already on. It is the OUTGOING image
    saying so, so it is not evidence -- set it anyway."""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_UNLOCKED_OLD,
                          fwreset_rc=1, config_query=_CONFIG_QUERY_UEFI_ON)
        assert _uefi_sets(calls), (
            "trusted a non-running image's config and skipped the UEFI "
            "enable.\ncalls:\n%s\n\nstdout:\n%s"
            % ("\n".join(calls), got.stdout))


def test_an_activated_card_that_already_has_the_rom_is_left_alone():
    """The converse, so the test above is not just 'always set it': when the
    card DID activate, a config reading True(1) is trustworthy."""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        got, calls = _run(pathlib.Path(tmp), dev_query=_DEV_QUERY_UNLOCKED_OLD,
                          fwreset_rc=0, config_query=_CONFIG_QUERY_UEFI_ON)
        assert not _uefi_sets(calls), (
            "needlessly re-set the ROM on a card that already had it: %s"
            % _uefi_sets(calls))
        assert "already set" in got.stdout, got.stdout
