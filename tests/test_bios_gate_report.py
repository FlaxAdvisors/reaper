import os
import stat
import subprocess
import time
import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..",
                      "roles", "apply_pxe_payloads", "files", "post",
                      "bios_gate_report.sh")

def _stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write("#!/bin/bash\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

def _run(tmp_path, verb, bios="TPC_P26C"):
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: %s"' % bios)
    _stub(d, "ssh", 'echo "%s reason text"' % verb)
    _stub(d, "shutdown", 'echo "SHUTDOWN CALLED" >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC="98039b875e32")
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    log = os.path.join(d, "shutdown.log")
    return r, os.path.exists(log)

def test_ack_triggers_shutdown(tmp_path):
    # NOTE: this asserts the script REACHED `shutdown`, not that it returned
    # promptly -- _run() sets FLAX_SHUTDOWN_WAIT=0. On a real node the ACK
    # path must not hand control back at all; see the blocking tests below.
    r, shut = _run(tmp_path, "ACK")
    assert r.returncode == 0
    assert shut is True

@pytest.mark.parametrize("verb", ["CLEAR", "HOLD", "NAK", "ERROR"])
def test_non_ack_never_shuts_down(tmp_path, verb):
    r, shut = _run(tmp_path, verb)
    assert r.returncode == 0
    assert shut is False

def test_ssh_failure_never_shuts_down(tmp_path):
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", "exit 255")
    _stub(d, "shutdown", 'echo x >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC="98039b875e32")
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert not os.path.exists(os.path.join(d, "shutdown.log"))

def test_unreadable_bios_version_never_shuts_down(tmp_path):
    d = str(tmp_path)
    _stub(d, "dmidecode", "exit 1")
    _stub(d, "ssh", 'echo "ACK should not matter"')
    _stub(d, "shutdown", 'echo x >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC="98039b875e32")
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert not os.path.exists(os.path.join(d, "shutdown.log"))


# --- Remote-side quoting -----------------------------------------------
#
# $mac and $biosver are interpolated into the string ssh hands to the
# remote shell, which re-parses it. These tests make the "ssh" stub behave
# like a real remote shell would: it takes the single command-string
# argument it was given and re-parses/executes it with `sh -c`, resolving
# a stub `bios_gate` on PATH -- exactly what sshd does server-side.
#
# Note: the script's own `dmidecode | sed ... (\S+)` extraction already
# stops a bare space from ever reaching $biosver (it captures only the
# first non-whitespace token) -- so a plain space alone can't reach the
# vulnerable point through $biosver. It CAN reach it through $mac, since
# FLAX_BOOTIF_MAC (the same env override these tests already use) is taken
# verbatim, bypassing the /proc/cmdline `[^ ]+` extraction entirely. And a
# shell metacharacter with NO whitespace (e.g. ";") sails straight through
# the \S+ filter untouched -- that is the sharper, realistic injection
# vector for $biosver. These tests cover all of it: a spaced dmidecode
# value (must degrade safely, not leak extra args), a spaced mac (the
# actually-reachable whitespace case), and a metacharacter-bearing bios
# version (must not inject a second remote command).

def _stub_remote_shell_ssh(d):
    # Simulates sshd invoking the remote login shell with -c "<command>":
    # take the last argv element (the single command string) and have a
    # POSIX shell parse/execute it, the way the real remote side would.
    _stub(d, "ssh", 'cmd="${@: -1}"; sh -c "$cmd"')

def _stub_bios_gate_recording(d, expect_argc, expect_value, position=None):
    # Records the argv it actually received after the remote shell
    # re-parsed the command string, and only answers ACK if it got
    # exactly the expected word count and the expected value at
    # `position` (1-based; default is the last/--current arg) --
    # proving no splitting occurred.
    if position is None:
        position = expect_argc
    body = (
        'echo "$#" > %s/argc.log\n'
        'for a in "$@"; do echo "$a" >> %s/argv.log; done\n'
        'if [ "$#" -eq %d ] && [ "$%d" = "%s" ]; then '
        'echo "ACK ok"; else echo "NAK bad-args"; fi'
    ) % (d, d, expect_argc, position, expect_value)
    _stub(d, "bios_gate", body)

def test_biosver_with_space_stays_safe_and_acks(tmp_path):
    d = str(tmp_path)
    # dmidecode's own \S+ extraction truncates this to "TPC" before it
    # ever reaches the ssh command -- verifying that degradation is safe
    # (single arg, no leakage of "P26C SPACED" as extra words) end to end.
    _stub(d, "dmidecode", 'echo "\tVersion: TPC P26C SPACED"')
    _stub_remote_shell_ssh(d)
    _stub_bios_gate_recording(d, 5, "TPC")
    _stub(d, "shutdown", 'echo "SHUTDOWN CALLED" >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC="98039b875e32")
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    with open(os.path.join(d, "argc.log")) as f:
        assert f.read().strip() == "5"
    with open(os.path.join(d, "argv.log")) as f:
        argv = f.read().splitlines()
    assert argv == ["report", "--mac", "98039b875e32", "--current", "TPC"]
    assert os.path.exists(os.path.join(d, "shutdown.log"))

def test_biosver_with_space_non_ack_still_no_shutdown(tmp_path):
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC P26C SPACED"')
    _stub_remote_shell_ssh(d)
    _stub(d, "bios_gate", 'echo "NAK reason"')
    _stub(d, "shutdown", 'echo x >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC="98039b875e32")
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert not os.path.exists(os.path.join(d, "shutdown.log"))

def test_mac_with_space_is_single_argument_and_acks(tmp_path):
    # FLAX_BOOTIF_MAC is taken verbatim (it bypasses the /proc/cmdline
    # `[^ ]+` extraction), so a space here is the genuinely reachable
    # whitespace-splitting case the reviewer flagged.
    d = str(tmp_path)
    mac = "98 039b875e32"
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub_remote_shell_ssh(d)
    _stub_bios_gate_recording(d, 5, "TPC_P26C")
    _stub(d, "shutdown", 'echo "SHUTDOWN CALLED" >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC=mac)
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    with open(os.path.join(d, "argc.log")) as f:
        assert f.read().strip() == "5"
    with open(os.path.join(d, "argv.log")) as f:
        argv = f.read().splitlines()
    assert argv == ["report", "--mac", mac, "--current", "TPC_P26C"]
    assert os.path.exists(os.path.join(d, "shutdown.log"))

def test_biosver_with_shell_metachar_no_injection(tmp_path):
    # No whitespace at all here, so \S+ captures it whole: "2.6;touch"
    # sails through the dmidecode extraction untouched. Unquoted, the
    # semicolon would end the `--current` value and start a second
    # command on the remote shell -- prove it does not.
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: 2.6;touch %s/pwned"' % d)
    _stub_remote_shell_ssh(d)
    _stub_bios_gate_recording(d, 5, "2.6;touch")
    _stub(d, "shutdown", 'echo "SHUTDOWN CALLED" >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC="98039b875e32")
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert not os.path.exists(os.path.join(d, "pwned"))
    with open(os.path.join(d, "argc.log")) as f:
        assert f.read().strip() == "5"
    with open(os.path.join(d, "argv.log")) as f:
        argv = f.read().splitlines()
    assert argv == ["report", "--mac", "98039b875e32", "--current", "2.6;touch"]
    assert os.path.exists(os.path.join(d, "shutdown.log"))


# --- /proc/cmdline BOOTIF fallback (FLAX_BOOTIF_MAC unset) --------------
#
# Every test above sets FLAX_BOOTIF_MAC, which bypasses the cmdline read
# entirely. These exercise that fallback path directly via FLAX_CMDLINE_FILE
# (a test-only override; production always defaults to /proc/cmdline).

def test_cmdline_with_bootif_yields_normalised_mac(tmp_path):
    d = str(tmp_path)
    cmdline = os.path.join(d, "cmdline")
    with open(cmdline, "w") as f:
        f.write("root=/dev/sda1 BOOTIF=01-98-03-9b-87-5e-32 quiet\n")
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", 'echo "ACK reason text"')
    _stub(d, "shutdown", 'echo "SHUTDOWN CALLED" >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_CMDLINE_FILE=cmdline)
    env.pop("FLAX_BOOTIF_MAC", None)
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "mac=98039b875e32" in r.stdout
    assert os.path.exists(os.path.join(d, "shutdown.log"))

def test_cmdline_without_bootif_yields_empty_mac_and_skips(tmp_path):
    d = str(tmp_path)
    cmdline = os.path.join(d, "cmdline")
    with open(cmdline, "w") as f:
        f.write("root=/dev/sda1 quiet splash\n")
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    # If the buggy sed regressed, this ssh stub would be reached and would
    # echo ACK -- the fix must never let it get this far.
    _stub(d, "ssh", 'echo "ACK reason text"')
    _stub(d, "shutdown", 'echo "SHUTDOWN CALLED" >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_CMDLINE_FILE=cmdline)
    env.pop("FLAX_BOOTIF_MAC", None)
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "no BOOTIF mac; skipping" in r.stdout
    assert not os.path.exists(os.path.join(d, "shutdown.log"))


# --- _sq() literal single-quote escaping --------------------------------

def test_literal_single_quote_in_mac_is_escaped_safely(tmp_path):
    # A classic quote-breakout payload: closes _sq()'s quoting, injects a
    # command, reopens quoting. If _sq() were broken this would execute
    # `touch <d>/PWNED` on the (simulated) remote shell.
    d = str(tmp_path)
    mac = "abc'; touch %s/PWNED; echo '" % d
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub_remote_shell_ssh(d)
    _stub_bios_gate_recording(d, 5, mac, position=3)
    _stub(d, "shutdown", 'echo "SHUTDOWN CALLED" >> %s/shutdown.log' % d)
    env = dict(os.environ, FLAX_SHUTDOWN_WAIT="0",
               PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC=mac)
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert not os.path.exists(os.path.join(d, "PWNED"))
    with open(os.path.join(d, "argc.log")) as f:
        assert f.read().strip() == "5"
    with open(os.path.join(d, "argv.log")) as f:
        argv = f.read().splitlines()
    assert argv == ["report", "--mac", mac, "--current", "TPC_P26C"]
    assert os.path.exists(os.path.join(d, "shutdown.log"))


# --- C1: ACK must not hand control back to post.sh ----------------------
#
# shutdown(8) is ASYNCHRONOUS: it schedules the halt and returns immediately.
# The old script did `shutdown -h now; exit 0`, and post.sh (lines 129-134)
# falls straight into ./update_mellanox.sh -- starting an mstflint/flint burn
# on a machine that is powering down. systemd kills it mid-write and the NIC
# is bricked, on a machine nobody can physically reach quickly. The spec says
# ACK means "skip inventory AND shut down"; the fix is to block here until the
# kernel takes the machine away. The wait is injectable (FLAX_SHUTDOWN_WAIT)
# so these tests can drive it to ~0.

def _ack_env(d, **extra):
    env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"],
               FLAX_BOOTIF_MAC="98039b875e32")
    env.pop("FLAX_SHUTDOWN_WAIT", None)
    env.update(extra)
    return env

def test_ack_blocks_after_shutdown_instead_of_returning(tmp_path):
    d = str(tmp_path)
    order = os.path.join(d, "order.log")
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", 'echo "ACK reason text"')
    _stub(d, "shutdown", 'echo shutdown >> %s' % order)
    _stub(d, "sleep", 'echo "sleep $1" >> %s' % order)
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)],
                       env=_ack_env(d, FLAX_SHUTDOWN_WAIT="7"),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    with open(order) as f:
        assert f.read().splitlines() == ["shutdown", "sleep 7"]

def test_default_shutdown_wait_is_long_not_a_token_pause(tmp_path):
    # The block has to outlast a real halt, not just look like a wait.
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", 'echo "ACK reason text"')
    _stub(d, "shutdown", 'true')
    _stub(d, "sleep", 'echo "$1" > %s/slept.log' % d)
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=_ack_env(d),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    with open(os.path.join(d, "slept.log")) as f:
        assert float(f.read().strip()) >= 300

def test_non_ack_never_blocks(tmp_path):
    # Every non-ACK path must return promptly so post.sh carries on.
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", 'echo "HOLD reason text"')
    _stub(d, "shutdown", 'true')
    _stub(d, "sleep", 'echo "$1" >> %s/slept.log' % d)
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=_ack_env(d),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert not os.path.exists(os.path.join(d, "slept.log"))

def test_caller_does_not_reach_the_nic_flash_while_shutdown_is_pending(tmp_path):
    # End to end, with the REAL sleep: a post.sh stand-in that calls the gate
    # script and then runs update_mellanox.sh must still be blocked when the
    # halt is only pending. Pre-fix this returned in milliseconds.
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", 'echo "ACK reason text"')
    _stub(d, "shutdown", 'echo shutdown >> %s/order.log' % d)      # async, returns
    caller = os.path.join(d, "post_stand_in.sh")
    with open(caller, "w") as f:
        f.write("#!/bin/bash\n"
                "bash %s || true\n"
                "echo nic_flash >> %s/order.log\n" % (os.path.abspath(SCRIPT), d))
    start = time.time()
    r = subprocess.run(["bash", caller],
                       env=_ack_env(d, FLAX_SHUTDOWN_WAIT="1"),
                       capture_output=True, text=True, timeout=30)
    elapsed = time.time() - start
    assert r.returncode == 0
    assert elapsed >= 1.0, "returned to the caller without waiting for the halt"
    with open(os.path.join(d, "order.log")) as f:
        assert f.read().splitlines() == ["shutdown", "nic_flash"]


# --- I5: the ssh call needs an overall time bound and -n ----------------
#
# -o ConnectTimeout bounds the TCP connect and nothing else. Server-side,
# bios_gate's resolve_port walks every switch port with a 10s GET each until
# the MAC matches, so a degraded port API blocks post.sh for minutes on every
# TiogaPass inventory boot.

def test_slow_agent_is_bounded_by_an_overall_timeout(tmp_path):
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", 'exec sleep 30')          # answers, eventually. Too late.
    _stub(d, "shutdown", 'echo x >> %s/shutdown.log' % d)
    start = time.time()
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)],
                       env=_ack_env(d, FLAX_GATE_SSH_TIMEOUT="1",
                                    FLAX_SHUTDOWN_WAIT="0"),
                       capture_output=True, text=True, timeout=30)
    elapsed = time.time() - start
    assert r.returncode == 0
    assert elapsed < 10, "the ssh call was not bounded (%.1fs)" % elapsed
    # No answer is a safe answer: never ACK, never power a node off.
    assert not os.path.exists(os.path.join(d, "shutdown.log"))

def test_default_ssh_timeout_is_bounded_and_finite(tmp_path):
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", 'for a in "$@"; do printf "%%s\\n" "$a" >> %s/argv.log; '
                    'done; echo "HOLD x"' % d)
    _stub(d, "shutdown", 'true')
    # A `timeout` wrapper must be in play even with nothing overridden: prove
    # it by checking the command actually runs under one.
    _stub(d, "timeout", 'echo "$1" > %s/timeout_arg.log; shift; exec "$@"' % d)
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=_ack_env(d),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    with open(os.path.join(d, "timeout_arg.log")) as f:
        assert float(f.read().strip()) > 0

def test_ssh_cannot_consume_the_callers_stdin(tmp_path):
    # ssh with an inherited stdin eats whatever post.sh still had queued.
    # Asserted BEHAVIOURALLY rather than by looking for "-n" in argv: the
    # script now also redirects < /dev/null, because `timeout` is not always
    # GNU coreutils and the uutils build silently swallows a -n anywhere in
    # the command's argv. The property is what matters, not the spelling.
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    # A deliberately greedy ssh: drains stdin the way a real ssh session does.
    _stub(d, "ssh", 'cat > %s/ssh_stdin.log; echo "HOLD x"' % d)
    _stub(d, "shutdown", 'true')
    caller = os.path.join(d, "post_stand_in.sh")
    with open(caller, "w") as f:
        f.write("#!/bin/bash\n"
                "bash %s > /dev/null || true\n"
                "read -r line\n"
                "echo \"STILL_MINE:$line\"\n" % os.path.abspath(SCRIPT))
    r = subprocess.run(["bash", caller], env=_ack_env(d), input="keepme\n",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    with open(os.path.join(d, "ssh_stdin.log")) as f:
        assert f.read() == "", "ssh consumed the caller's stdin"
    assert "STILL_MINE:keepme" in r.stdout

def test_ssh_still_carries_the_connect_timeout_and_dash_n(tmp_path):
    # -n stays in the command line: it is the ssh-native spelling, and it does
    # survive `timeout`'s argument handling. The < /dev/null redirection is the
    # shell-level backstop, covered behaviourally by the test above.
    # NB printf, not echo: `echo "-n"` silently swallows the very argument
    # under test.
    d = str(tmp_path)
    _stub(d, "dmidecode", 'echo "\tVersion: TPC_P26C"')
    _stub(d, "ssh", 'for a in "$@"; do printf "%%s\\n" "$a" >> %s/argv.log; '
                    'done; echo "HOLD x"' % d)
    _stub(d, "shutdown", 'true')
    r = subprocess.run(["bash", os.path.abspath(SCRIPT)], env=_ack_env(d),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    with open(os.path.join(d, "argv.log")) as f:
        argv = f.read().splitlines()
    assert "-n" in argv
    assert "-o" in argv and "ConnectTimeout=10" in argv
