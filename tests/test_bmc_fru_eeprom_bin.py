# tests/test_bmc_fru_eeprom_bin.py
"""scripts/bmc-fru-eeprom.sh.j2: the rendered bin, driven through its
FLAX_BMC_REMOTE_EXEC hook. The fake BMC really runs each remote command, with
the EEPROM path pointed at a fixture file, so read / stage / write / verify
are exercised for real (spec 2026-10-05-triage-fru-fw-design §5.1).
"""
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REAPER = Path(__file__).resolve().parents[1]
TEMPLATE = REAPER / "scripts" / "bmc-fru-eeprom.sh.j2"
EEPROM = "/sys/bus/i2c/devices/6-0054/eeprom"

# BusyBox hexdump, as the bin calls it (`hexdump -v -e '1/1 "%02x"' FILE`): a
# stand-in so the test does not depend on the host having one, and never
# skips silently.
_FAKE_HEXDUMP = """#!/usr/bin/env python3
import sys
sys.stdout.write(open(sys.argv[-1], "rb").read().hex())
"""

# The fixture "BMC": rewrites the EEPROM and /tmp paths into the test dir and
# runs the command. FAKE_FAIL=<n> makes the n-th call exit 255 (ssh failure);
# FAKE_CORRUPT=1 flips a byte of whatever is written to the chip.
_FAKE_EXEC = r"""#!/bin/bash
n=$(( $(cat "$FAKE_DIR/calls" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_DIR/calls"
[ "${FAKE_FAIL:-0}" = "$n" ] && exit 255
cmd=$(printf '%s' "$2" | sed -e "s#/tmp/#@TMP@#g" -e "s#/sys/bus/i2c/devices/6-0054/eeprom#$FAKE_DIR/eeprom#g" -e "s#@TMP@#$FAKE_DIR/tmp-#g")
[ -n "${FAKE_HANG:-}" ] && sleep 60
[ -e /proc/$$/fd/9 ] && echo leaked > "$FAKE_DIR/fd9-leaked"
if [ -n "${FAKE_STAGE_CORRUPT:-}" ]; then
    # the staged copy on the BMC differs from what was sent
    cmd=$(printf '%s' "$cmd" | sed -e "s#cat > \\($FAKE_DIR/tmp-fru-new.bin\\) &&#{ cat; printf Z; } > \\1 \\&\\&#")
fi
if [ -n "${FAKE_CORRUPT:-}" ]; then
    # a chip that takes the write but reads back one byte different
    cmd=$(printf '%s' "$cmd" | sed -e "s#; echo CHIP=#; printf 'Z' | dd of=$FAKE_DIR/eeprom bs=1 seek=100 conv=notrunc 2>/dev/null; echo CHIP=#")
fi
PATH="$FAKE_DIR/bin:$PATH" bash -c "$cmd"
rc=$?
exit $rc
"""


@pytest.fixture
def rig(tmp_path):
    binp = tmp_path / "bmc-fru-eeprom"
    binp.write_text(TEMPLATE.read_text().replace("{{ bmc_root_password | quote }}", "'test-dummy'"))
    binp.chmod(binp.stat().st_mode | stat.S_IXUSR)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "bin").mkdir()
    busctl = fake / "bin" / "busctl"
    busctl.write_text('#!/bin/bash\necho "$@" > "%s/busctl-args"\n' % fake)
    busctl.chmod(0o755)
    hexdump = fake / "bin" / "hexdump"
    hexdump.write_text(_FAKE_HEXDUMP)
    hexdump.chmod(0o755)
    sleep = fake / "bin" / "sleep"
    sleep.write_text("#!/bin/bash\nexit 0\n")
    sleep.chmod(0o755)
    exe = tmp_path / "exec"
    exe.write_text(_FAKE_EXEC)
    exe.chmod(0o755)
    chip = bytes(range(256)) * 32
    (fake / "eeprom").write_bytes(chip)
    env = dict(os.environ, FLAX_BMC_REMOTE_EXEC=str(exe), FAKE_DIR=str(fake),
               FW_UPDATE_LOCK_DIR=str(tmp_path / "locks"))

    def run(*args, **extra):
        p = subprocess.run([str(binp)] + list(args), env=dict(env, **extra),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])
    return run, fake, chip, tmp_path


def md5(b):
    return hashlib.md5(b).hexdigest()


def test_template_carries_no_literal_credential():
    text = TEMPLATE.read_text()
    assert "export SSHPASS={{ bmc_root_password | quote }}" in text
    assert text.count("SSHPASS=") == 1


def test_read_returns_the_chip_byte_for_byte(rig):
    run, fake, chip, tmp = rig
    out = tmp / "out.bin"
    rc, j = run("read", "10.0.0.1", str(out))
    assert rc == 0 and j == {"read": True, "size": 8192, "md5": md5(chip)}
    assert out.read_bytes() == chip


def test_read_refuses_a_chip_of_another_size_or_no_chip(rig):
    run, fake, chip, tmp = rig
    (fake / "eeprom").write_bytes(chip[:4096])
    assert run("read", "10.0.0.1", str(tmp / "o"))[1] == {"error": "bad_size"}
    (fake / "eeprom").unlink()
    assert run("read", "10.0.0.1", str(tmp / "o"))[1] == {"error": "no_eeprom"}


def test_read_reports_an_unreachable_bmc(rig):
    run, fake, chip, tmp = rig
    rc, j = run("read", "10.0.0.1", str(tmp / "o"), FAKE_FAIL="1")
    assert rc == 1 and j == {"error": "ssh_unreachable"}


def test_write_puts_the_image_on_the_chip_and_verifies(rig):
    run, fake, chip, tmp = rig
    new = bytes(reversed(chip))
    src = tmp / "new.bin"
    src.write_bytes(new)
    rc, j = run("write", "10.0.0.1", str(src))
    assert rc == 0 and j == {"staged_ok": True, "verified": True, "md5": md5(new)}
    assert (fake / "eeprom").read_bytes() == new
    assert (fake / "tmp-fru-6-54.bak").read_bytes() == chip      # old image kept on the BMC


def test_write_refuses_a_file_that_is_not_8192_bytes(rig):
    run, fake, chip, tmp = rig
    src = tmp / "short.bin"
    src.write_bytes(b"\x00" * 100)
    assert run("write", "10.0.0.1", str(src))[1] == {"error": "bad_file"}
    assert (fake / "eeprom").read_bytes() == chip


def test_write_reports_a_chip_that_does_not_read_back(rig):
    run, fake, chip, tmp = rig
    src = tmp / "new.bin"
    src.write_bytes(bytes(reversed(chip)))
    rc, j = run("write", "10.0.0.1", str(src), FAKE_CORRUPT="1")
    assert rc == 1 and j["staged_ok"] is True and j["verified"] is False


def test_write_never_touches_the_chip_when_the_bmc_is_unreachable(rig):
    run, fake, chip, tmp = rig
    src = tmp / "new.bin"
    src.write_bytes(bytes(reversed(chip)))
    rc, j = run("write", "10.0.0.1", str(src), FAKE_FAIL="1")
    assert rc == 1 and j == {"error": "ssh_unreachable"}
    assert (fake / "eeprom").read_bytes() == chip


def test_write_is_refused_while_the_firmware_lock_is_held(rig):
    run, fake, chip, tmp = rig
    src = tmp / "new.bin"
    src.write_bytes(bytes(reversed(chip)))
    locks = tmp / "locks"
    locks.mkdir(exist_ok=True)
    holder = subprocess.Popen(["flock", str(locks / "fw-update-10.0.0.1.lock"), "sleep", "20"])
    try:
        import time
        time.sleep(0.5)
        rc, j = run("write", "10.0.0.1", str(src))
        assert rc == 1 and j == {"error": "busy"}
        assert (fake / "eeprom").read_bytes() == chip
    finally:
        holder.kill()


def test_reread_calls_the_fru_device_rescan(rig):
    run, fake, chip, tmp = rig
    rc, j = run("reread", "10.0.0.1")
    assert rc == 0 and j == {"rescanned": True}
    assert "xyz.openbmc_project.FruDeviceManager ReScan" in (fake / "busctl-args").read_text()


# --- final review fixes (2026-10-06) ----------------------------------------
def test_gate_1_a_staged_copy_that_differs_never_reaches_the_chip(rig):
    run, fake, chip, tmp = rig
    src = tmp / "new.bin"
    src.write_bytes(bytes(reversed(chip)))
    rc, j = run("write", "10.0.0.1", str(src), FAKE_STAGE_CORRUPT="1")
    assert rc == 1 and j == {"error": "stage_mismatch"}
    assert (fake / "eeprom").read_bytes() == chip


def test_the_remote_command_never_inherits_the_lock(rig):
    # an ssh that outlives a killed bin must not keep the per-BMC fw lock
    run, fake, chip, tmp = rig
    src = tmp / "new.bin"
    src.write_bytes(bytes(reversed(chip)))
    assert run("write", "10.0.0.1", str(src))[0] == 0
    assert not (fake / "fd9-leaked").exists()


def test_a_bmc_that_stops_answering_is_cut_off(rig):
    import time
    run, fake, chip, tmp = rig
    t0 = time.time()
    rc, j = run("read", "10.0.0.1", str(tmp / "o"), FAKE_HANG="1", FRU_SSH_TIMEOUT="2")
    assert rc == 1 and j == {"error": "ssh_unreachable"}
    assert time.time() - t0 < 20
