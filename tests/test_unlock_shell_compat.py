"""The station script must be able to actually call into common_mellanox.sh.

Regression for the first live run (2026-09-18): unlock_mellanox.sh ran with
`set -u`, and common_mellanox.sh's domstflint() assigns `binfile=$3` while
getdevinfo() calls it with only two arguments. Under -u that is fatal, so
EVERY card query died with

    ./common_mellanox.sh: line 75: $3: unbound variable
    5e:00.0: query failed; skipping.

and three consecutive runs flashed nothing while still blinking IDENT --
"done" is the station's only signal, so the rack showed success. The jumpered
card went back into service still locked.

common_mellanox.sh is shared VERBATIM with the post lane and must not be
edited to suit the station, so the fix belongs here: the station script runs
without -u, exactly as update_mellanox.sh (#!/bin/bash -x) always has.
"""
import os
import subprocess

_REAPER = os.path.join(os.path.dirname(__file__), os.pardir)
_COMMON = os.path.join(_REAPER, "roles", "apply_pxe_payloads", "files", "post",
                       "common_mellanox.sh")
_STATION = os.path.join(_REAPER, "scripts", "unlock-with-ubuntu", "payload",
                        "unlock_mellanox.sh")

_FAKE_MSTFLINT = """#!/bin/bash
# Enough of `mstflint -d <dev> query` for getdevinfo to parse.
cat <<'EOF'
Image type:            FS4
FW Version:            14.32.1912
PSID:                  MT_2420110034
Security Attributes:   secure-fw
EOF
"""


def _run_getdevinfo(tmp_path, shell_opts):
    """Source common_mellanox.sh under `shell_opts` and call getdevinfo."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "mstflint"
    fake.write_text(_FAKE_MSTFLINT)
    fake.chmod(0o755)
    script = "%s\nsource %s\ngetdevinfo 5e:00.0\n" % (shell_opts, _COMMON)
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]))
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=env, cwd=str(tmp_path))


def test_set_u_is_what_broke_the_live_run(tmp_path):
    """Prove the diagnosis: with -u, querying a card fails on common's $3."""
    got = _run_getdevinfo(tmp_path, "set -u")
    assert "unbound variable" in got.stderr, (
        "expected the -u failure this test exists to document; got: %r" % got.stderr)


def test_the_station_script_queries_a_card_successfully(tmp_path):
    """The real assertion: under the options unlock_mellanox.sh actually sets,
    getdevinfo parses a card instead of dying."""
    with open(_STATION) as f:
        station = f.read()
    opts = "\n".join(ln for ln in station.splitlines()
                     if ln.strip().startswith("set -"))
    got = _run_getdevinfo(tmp_path, opts)
    assert "unbound variable" not in got.stderr, (
        "unlock_mellanox.sh's shell options break common_mellanox.sh: %s" % got.stderr)
    assert got.returncode == 0, "getdevinfo failed: %s" % got.stderr
    assert "devfw 14.32.1912" in got.stdout, got.stdout
    assert "devpsid MT_2420110034" in got.stdout, got.stdout


def test_identify_is_lit_before_the_node_powers_off():
    """Ordering, mirroring post.sh: identify, THEN power off. Reversed, the
    blade powers down before the LED is set and the operator has no signal at
    all -- and blink-means-done is the station's only output.

    post.sh uses `ipmitool chassis power off` (immediate) because it runs from
    a RAM-based live ISO with no disk to dirty. The station has a real rootfs
    that is otherwise hard-cut on every pull, so it must come down cleanly.
    """
    with open(_STATION) as f:
        # Comments discuss the hard-off we deliberately avoid, so judge the
        # code, not the prose explaining it.
        body = "\n".join(ln for ln in f.read().splitlines()
                         if not ln.lstrip().startswith("#"))
    ident = body.index("chassis identify force")
    assert "shutdown -h now" in body, "station must power off cleanly, not hard-off"
    assert "ipmitool chassis power off" not in body, (
        "an immediate IPMI power off would dirty the rootfs -- the whole "
        "reason for shutting down at all")
    assert ident < body.index("shutdown -h now"), (
        "identify must be set before the node powers off")


def test_shutdown_is_gated_by_config_so_dev_can_keep_the_node_up():
    """Reading the log after a run is only possible while the node is up; the
    knob is what made today's diagnosis possible at all."""
    with open(_STATION) as f:
        body = f.read()
    assert "SHUTDOWN_ON_DONE" in body
    assert "/etc/flax/mezz-flash.conf" in body, (
        "the script must source the config it reads SHUTDOWN_ON_DONE from")
