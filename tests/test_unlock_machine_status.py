"""The station's state is machine-readable, for a triage mezz_flash agent.

Operator intent 2026-09-22: a tile in the triage UI should show the station's
state without anyone opening SOL. Two surfaces, both written by the station:

  1. The banner's status line (second line, after the title):
         <ISO-8601 UTC>  <STATE>  <human text>
     STATE is a fixed token, never prose: RUNNING, DONE, REMOVE-JUMPER,
     FIT-JUMPER, MIXED, PROBLEM. The operator wording stays in the text.
  2. A JSON status file, for an agent that can reach the node (every
     jumper-less run: a jumpered card leaves the BMC and NIC dark).
"""
import json
import os
import pathlib
import re
import tempfile

from test_unlock_rack_signal import (
    _FINISHED,
    _JUMPERED_LOCKED,
    _UNJUMPERED_LOCKED,
    _UNLOCKED_UEFI_OFF,
    run,
)

STATUS_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)  "
    r"(?P<state>RUNNING|DONE|REMOVE-JUMPER|FIT-JUMPER|MIXED|PROBLEM|STATION-FAULT)  "
    r"(?P<text>\S.*)$")


def _status_line(issue):
    """Last line before the closing rule: the status line sits at the bottom
    so it cannot scroll off a SOL console."""
    lines = [l for l in issue.splitlines() if l.strip()]
    i = [n for n, l in enumerate(lines) if l.startswith("=====")][-1]
    return lines[i - 1]


def test_the_status_line_parses_with_a_state_token():
    for kw, state in ((_FINISHED, "DONE"),
                      (_UNLOCKED_UEFI_OFF, "DONE"),
                      (_JUMPERED_LOCKED, "REMOVE-JUMPER"),
                      (_UNJUMPERED_LOCKED, "FIT-JUMPER")):
        r = run(**kw)
        line = _status_line(r.issue)
        m = STATUS_LINE.match(line)
        assert m, (kw, line)
        assert m.group("state") == state, (state, line)


def test_the_operator_wording_survives_in_the_text():
    assert "FIT THE JUMPER" in _status_line(run(**_UNJUMPERED_LOCKED).issue)
    assert "REMOVE THE JUMPER" in _status_line(run(**_JUMPERED_LOCKED).issue)


def test_a_problem_parses_too():
    r = run(burn_refused=True, **_JUMPERED_LOCKED)
    m = STATUS_LINE.match(_status_line(r.issue))
    assert m and m.group("state") == "PROBLEM", _status_line(r.issue)


def test_the_status_file_carries_the_state_and_the_card():
    r = run(**_FINISHED)
    d = json.loads(r.status)
    assert d["state"] == "DONE", d
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", d["ts"]), d
    card = d["cards"][0]
    assert card["mac"] == "506b4bf9e416", card
    assert card["verdict"] == "done" and card["bootrom"] == "already", card
    assert card["lock"] == "cleared" and card["jumper"] == "no", card
    assert card["psid"] == "MT_2450112034" and card["fw"] == "14.32.1912", card


def test_the_status_file_says_which_jumper_state_is_needed():
    d = json.loads(run(**_JUMPERED_LOCKED).status)
    assert d["state"] == "REMOVE-JUMPER", d
    assert d["cards"][0]["jumper"] == "yes", d
    assert d["cards"][0]["lock"] == "locked", d
    d2 = json.loads(run(**_UNJUMPERED_LOCKED).status)
    assert d2["state"] == "FIT-JUMPER" and d2["cards"][0]["jumper"] == "no", d2


def test_the_status_file_exists_while_the_run_is_still_going():
    """A tile should show RUNNING, not go blank for the 3 min of a burn."""
    with tempfile.TemporaryDirectory() as tmp:
        from test_unlock_uefi_enable import _bundle
        import subprocess
        d, bindir = _bundle(pathlib.Path(tmp), **_JUMPERED_LOCKED)
        status = d / "status.json"
        snap = d / "status_at_burn.json"
        f = bindir / "mstflint"
        f.write_text(f.read_text().replace(
            'for a in "$@"; do [ "$a" = "burn" ] && {',
            'for a in "$@"; do [ "$a" = "burn" ] && {\n'
            '        cp "%s" "%s"' % (status, snap), 1))
        logdir = pathlib.Path(tmp) / "logs"
        logdir.mkdir()
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   FAKEDIR=str(d), MEZZ_FLASH_ALL="1", SHUTDOWN_ON_DONE="1",
                   MEZZ_SOL_DEV=str(d / "sol.out"),
                   MEZZ_ISSUE=str(d / "mezz-flash.issue"),
                   MEZZ_STATUS=str(status), MEZZ_LOGDIR=str(logdir))
        subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                       capture_output=True, text=True, env=env, cwd=str(d))
        mid = json.loads(snap.read_text())
        assert mid["state"] == "RUNNING", mid
        assert "BURNING" in mid["text"], mid


_ROW = ("a: RESULT mac=%s guid=x opn=MCX4411A-ACQ psid=MT_2450112034 "
        "fw=14.32.1912 sec=[N/A] burned=no bootaddr=n/a activated=n/a "
        "resetrc=n/a bootrom=already romset=none livefish=no verdict=done\n")
_PREFIX_ROW = ("a: RESULT mac=506b4bf9old9 guid=x opn=unknown "
               "psid=FB_0000000005 fw=14.32.1206 sec=[secure-fw] burned=yes "
               "bootaddr=failed activated=no resetrc=1 uefi=set\n")


def test_the_status_file_carries_recent_history():
    """A jumpered run is invisible: no SOL, no BMC, no NIC. Its outcome can
    only be reported on the NEXT jumper-less boot, so the agent needs the
    recent rows, not just the current state."""
    d = json.loads(run(history=_ROW % "506b4bf9hh01", **_FINISHED).status)
    assert d["history"], d
    h = d["history"][0]
    assert h["mac"] == "506b4bf9hh01" and h["verdict"] == "done", h
    assert h["bootrom"] == "already" and h["lock"] == "cleared", h
    assert h["verified"] is True, h
    assert re.match(r"\d{8}T\d{6}Z", h["when"]), h


def test_prefix_history_rows_are_marked_unverified_in_json():
    d = json.loads(run(history=_PREFIX_ROW, **_FINISHED).status)
    h = [x for x in d["history"] if x["mac"] == "506b4bf9old9"][0]
    assert h["verified"] is False, h
    assert h["verdict"] == "unknown" and h["lock"] == "locked", h


def test_history_in_json_is_newest_first_and_capped():
    import subprocess
    from test_unlock_uefi_enable import _bundle
    with tempfile.TemporaryDirectory() as tmp:
        d, bindir = _bundle(pathlib.Path(tmp), **_FINISHED)
        logdir = pathlib.Path(tmp) / "logs"
        logdir.mkdir()
        for n in range(8):
            f = logdir / ("20260922T0%d0000Z.log" % n)
            f.write_text(_ROW % ("506b4bf9r%03d" % n))
            os.utime(f, (100 + n, 100 + n))
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   FAKEDIR=str(d), MEZZ_FLASH_ALL="1", SHUTDOWN_ON_DONE="1",
                   MEZZ_SOL_DEV=str(d / "sol.out"),
                   MEZZ_ISSUE=str(d / "mezz-flash.issue"),
                   MEZZ_STATUS=str(d / "status.json"), MEZZ_LOGDIR=str(logdir))
        subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                       capture_output=True, text=True, env=env, cwd=str(d))
        got = json.loads((d / "status.json").read_text())
        macs = [h["mac"] for h in got["history"]]
        assert macs == ["506b4bf9r007", "506b4bf9r006", "506b4bf9r005",
                        "506b4bf9r004", "506b4bf9r003"], macs


def test_the_status_file_identifies_a_station_and_its_bundle():
    """So the agent latches 'check power + IDENT' only for station slots."""
    d = json.loads(run(**_FINISHED).status)
    assert d["station"] is True, d
    assert d["bundle"], d


def test_status_json_carries_the_station_ident():
    r = run(**_FINISHED)
    doc = json.loads(r.status)
    assert doc["ident"]["station_sn"]
    assert doc["ident"]["serial_field"]
    assert doc["ident"]["family"]


def test_ident_is_read_once_not_per_progress_step():
    # station_ident.py (ipmitool fru under the hood) is slow and the banner
    # updates at every step. Reading it per step would add seconds to every
    # burn for no new information -- the blade cannot change identity
    # mid-run. Count invocations of the stub directly: it never shells out
    # to ipmitool itself, so grepping the calls log for "ipmitool ... fru"
    # would never see anything and the assertion could never fail.
    r = run(**_FINISHED)
    ident_calls = [c for c in r.calls if c.startswith("station_ident.py")]
    assert len(ident_calls) <= 1, r.why()


def test_station_fault_is_a_distinct_token_from_problem():
    # PROBLEM is a CARD verdict. A blade that cannot identify itself is a
    # BLADE fault, and overloading PROBLEM would make the tile unable to tell
    # "this card failed" from "this station is not a station".
    r = run(no_ident=True, **_FINISHED)
    assert " STATION-FAULT " in r.issue, r.why()
    assert "mstflint" not in " ".join(r.calls), "must refuse to flash: " + r.why()
    # The rack signal must mirror PROBLEM exactly (IDENT off, left powered
    # on) -- a powered-off blade would read at the rack as a jumper-flip
    # verdict, which means something completely different to the operator.
    assert not r.powered_off and not r.blinking, r.why()
