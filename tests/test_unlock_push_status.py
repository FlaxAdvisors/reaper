"""The station PUSHES its status; nothing polls it.

A station powers itself off seconds after DONE (SHUTDOWN_ON_DONE=1), so a
poller would reliably miss the one state an operator cares about. Its only NIC
is the card under test, swapped every cycle, so it has no stable address
either -- its host lease is routinely from the free pool.

A push must NEVER fail a run: flashing is the job, reporting is not. And a
jumpered run is offline for its whole duration by design, so the failure path
has to be cheap.
"""
import json
import os
import pathlib
import subprocess
import tempfile

from test_unlock_rack_signal import _FINISHED, _JUMPERED_LOCKED, run
from test_unlock_uefi_enable import _bundle


def _pushes(r):
    return [c for c in r.calls if c.startswith("curl")]


def _run_with_env_overlay(bindir_files=None, ident_stub=None, **kw):
    """Like test_unlock_rack_signal.run(), but lets a test drop extra fake
    binaries into bindir or replace the station_ident.py stub before the
    subprocess actually runs -- neither of which the plain `run()` harness
    exposes."""
    with tempfile.TemporaryDirectory() as tmp:
        d, bindir = _bundle(pathlib.Path(tmp), **kw)
        for name, text in (bindir_files or {}).items():
            f = bindir / name
            f.write_text(text)
            f.chmod(0o755)
        if ident_stub is not None:
            (d / "station_ident.py").write_text(ident_stub)
            (d / "station_ident.py").chmod(0o755)
        logdir = pathlib.Path(tmp) / "logs"
        logdir.mkdir()
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   FAKEDIR=str(d), MEZZ_FLASH_ALL="1", SHUTDOWN_ON_DONE="1",
                   MEZZ_SOL_DEV=str(d / "sol.out"),
                   MEZZ_ISSUE=str(d / "mezz-flash.issue"),
                   MEZZ_STATUS=str(d / "status.json"), MEZZ_LOGDIR=str(logdir))
        proc = subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                              capture_output=True, text=True, env=env,
                              cwd=str(d))
        calls = (d / "calls.log").read_text().splitlines()
        status = (d / "status.json")
        return proc, calls, (status.read_text() if status.exists() else "")


def test_every_state_change_is_pushed():
    r = run(**_FINISHED)
    assert len(_pushes(r)) >= 2, r.why()      # progress steps + the verdict


def test_the_pushed_body_is_the_status_file_itself():
    # --data-binary @<file>, not a second serialisation: the pushed bytes and
    # the on-disk file cannot disagree.
    r = run(**_FINISHED)
    assert any("--data-binary" in c and "@" in c for c in _pushes(r)), r.why()


def test_a_failing_push_never_fails_the_run():
    r = run(curl_fails=True, **_FINISHED)
    # Without this, a pushstatus() that never calls curl at all would still
    # pass: "never fails the run" is meaningless if no push was ever tried.
    assert _pushes(r), "no pushes recorded: " + r.why()
    assert r.proc.returncode == 0, r.why()
    assert r.blinking, "DONE must still light IDENT: " + r.why()


def test_a_failing_push_stops_after_two_attempts():
    # _JUMPERED_LOCKED actually burns, which adds a mid-run "BURNING
    # firmware" push between the 2 that trip the latch and the re-armed
    # final verdict push -- the only place the latch has anything to
    # suppress. _FINISHED (an already-done card) produces exactly 2
    # pre-latch pushes plus the 1 re-armed final one = 3 either way, so a
    # correct implementation and a latch-removed one are indistinguishable
    # on it. A jumpered run is offline for its whole duration; without the
    # latch, ~15 progress steps x 2s would add ~30s to every flash.
    r = run(curl_fails=True, **_JUMPERED_LOCKED)
    assert len(_pushes(r)) == 3, "unbounded retry: " + r.why()


def test_the_push_is_time_bounded():
    r = run(**_FINISHED)
    # Without this, an empty _pushes(r) -- i.e. the feature broken or
    # missing -- makes all(...) vacuously True and the test passes for
    # exactly the wrong reason.
    assert _pushes(r), "no pushes recorded: " + r.why()
    assert all("--max-time" in c and "--connect-timeout" in c
               for c in _pushes(r)), r.why()


def test_a_jumpered_run_still_completes_with_no_network():
    r = run(curl_fails=True, **_JUMPERED_LOCKED)
    assert _pushes(r), "no pushes recorded: " + r.why()
    assert r.proc.returncode == 0, r.why()


# --- I3: pushurl must not be frozen at startup ------------------------------

_LATE_ROUTE_IP = (
    '#!/bin/sh\n'
    'echo "$(basename "$0") $*" >> "$FAKEDIR/calls.log"\n'
    'n=$(cat "$FAKEDIR/ip_calls" 2>/dev/null || echo 0)\n'
    'n=$((n+1))\n'
    'echo "$n" > "$FAKEDIR/ip_calls"\n'
    '# No route on the very first call (the unit starting before DHCP has a\n'
    '# lease -- the unit is ordered only After=multi-user.target getty.target,\n'
    '# deliberately no network dependency). A real route from every call after.\n'
    'if [ "$n" -ge 2 ]; then\n'
    '    echo "default via 172.16.0.1 dev eth0 proto dhcp"\n'
    'fi\n'
)


def test_a_late_arriving_default_route_still_gets_pushed():
    """I3: pushgw/pushurl used to be computed ONCE at the top of the script.
    A jumper-less station whose lease has not arrived yet at that point
    freezes pushurl empty for the rest of the run -- including the final
    verdict -- with no way to recover it. Simulate DHCP completing mid-run
    (no route on the 1st `ip` call, a real one on every call after) and
    check a push still happens once the route exists."""
    proc, calls, _ = _run_with_env_overlay(
        bindir_files={"ip": _LATE_ROUTE_IP}, **_FINISHED)
    pushes = [c for c in calls if c.startswith("curl")]
    assert pushes, "no pushes after the route appeared:\n" + "\n".join(calls)


def test_a_route_present_from_the_start_is_still_used_once():
    """The re-probe must not keep calling `ip` forever once a URL is in
    hand -- only while pushurl is still empty."""
    always_route = (
        '#!/bin/sh\n'
        'echo "$(basename "$0") $*" >> "$FAKEDIR/calls.log"\n'
        'echo "default via 172.16.0.1 dev eth0 proto dhcp"\n'
    )
    proc, calls, _ = _run_with_env_overlay(
        bindir_files={"ip": always_route}, **_FINISHED)
    pushes = [c for c in calls if c.startswith("curl")]
    ip_calls = [c for c in calls if c.startswith("ip ")]
    assert pushes, "no pushes recorded:\n" + "\n".join(calls)
    # One `ip` call to seed pushgw at startup, then never again once pushurl
    # is non-empty -- not one per push.
    assert len(ip_calls) == 1, ip_calls


# --- S1: via-aware default-route parsing ------------------------------------

def test_a_scope_link_default_route_derives_no_bad_url():
    """A scope-link default route ("default dev eth0", no gateway IP) has no
    third field for the old `awk '{print $3; exit}'` to grab -- it read the
    INTERFACE NAME instead and built "http://eth0:5555/...". The via-aware
    parse must come back empty (and the lazy I3 re-probe keeps retrying,
    same as no route at all) rather than push to a bogus host."""
    scope_link_ip = (
        '#!/bin/sh\n'
        'echo "$(basename "$0") $*" >> "$FAKEDIR/calls.log"\n'
        'echo "default dev eth0 scope link"\n'
    )
    proc, calls, _ = _run_with_env_overlay(
        bindir_files={"ip": scope_link_ip}, **_FINISHED)
    pushes = [c for c in calls if c.startswith("curl")]
    assert not any("eth0:5555" in c for c in pushes), pushes


# --- I6: the station's own stderr must reach the run log -------------------

_TALKATIVE_FAILING_IDENT = (
    '#!/bin/sh\n'
    'echo "$(basename "$0") $*" >> "$FAKEDIR/calls.log"\n'
    'echo \'{"station_sn":"","serial_field":"Chassis Serial","family":"leopard",'
    '"state":"no_serial","bmc_mac":"","bmc_ip":"","host_mac":"","host":"x"}\'\n'
    'echo "station_ident: no_serial -- FRU 0 has no Chassis Serial for family leopard." >&2\n'
    'exit 1\n'
)


def test_the_stations_own_stderr_reaches_the_run_log():
    """I6: unlock_mellanox.sh used to call station_ident.py with 2>/dev/null,
    throwing away the only message saying which field was empty for which
    family. The script already tees everything to the run log and SOL
    (`exec > >(tee -a "$log") 2>&1`), so dropping the redirect costs nothing
    and the reason must show up in the captured run output."""
    proc, calls, _ = _run_with_env_overlay(
        ident_stub=_TALKATIVE_FAILING_IDENT, **_FINISHED)
    combined = proc.stdout + proc.stderr
    assert "no Chassis Serial for family leopard" in combined, combined


# --- S3: the identjson fallback must be the full shape ----------------------

def test_a_crashed_identity_helper_still_yields_a_full_shape_fallback():
    """S3: the old fallback '{"station_sn":"","state":"no_serial"}' was
    missing serial_field/family/bmc_mac/bmc_ip/host_mac/host. The consumer
    of status.json's `ident` block requires a usable object; a plain key
    lookup on any of those missing keys KeyErrors. Force station_ident.py to
    crash with NO stdout at all (harder than the normal no_serial JSON) and
    check the fallback status.json still carries every key."""
    crash_silently = '#!/bin/sh\nexit 1\n'
    proc, calls, status_text = _run_with_env_overlay(
        ident_stub=crash_silently, **_FINISHED)
    assert status_text, "no status.json written: " + proc.stdout
    status = json.loads(status_text)
    ident = status["ident"]
    for key in ("station_sn", "serial_field", "family", "state",
               "bmc_mac", "bmc_ip", "host_mac", "host"):
        assert key in ident, (key, ident)


# --- S5: the STATION-FAULT IDENT-off call must be silenced like its twin ---

def test_the_station_fault_ident_off_call_is_silenced():
    """S5: `ipmitool chassis identify 0 || true` in the STATION-FAULT path
    was unsilenced unlike the identical call earlier in the normal-run path
    (`>/dev/null 2>&1 || true`) -- cosmetic, but the two calls doing the same
    thing (turn IDENT off) should look the same in the source."""
    reaper_root = pathlib.Path(__file__).resolve().parents[1]
    src = reaper_root / "scripts" / "unlock-with-ubuntu" / "payload" / "unlock_mellanox.sh"
    text = src.read_text()
    idx = text.index('state=STATION-FAULT')
    fault_block = text[idx:idx + 1500]
    assert 'ipmitool chassis identify 0 >/dev/null 2>&1 || true' in fault_block, (
        fault_block)
