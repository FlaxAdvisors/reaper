"""The station's verdict reaches the rack as power state + IDENT, and SOL.

An operator notes at most whether the node is powered and whether IDENT is
blinking, and can sometimes be persuaded to open the triage SOL. So every run
ends in exactly one of three node verdicts (operator decision 2026-09-22):

  DONE     IDENT blinking. Every card is unlocked with its UEFI ROM on.
           Powers off only if this run changed a card; a no-op run stays up.
  FLIP     IDENT off, powered off. Not finished: flip the jumper state and run
           again -- REMOVE it after a jumpered burn, FIT it for a card that is
           still locked. The operator knows which state they left it in.
  PROBLEM  IDENT off, stays up. Something failed; SOL says what.

Why FLIP exists, from 40 live runs on et9b1/et9b4 (2026-09-21/22):
  - Jumper fitted = card in livefish. mstflint burns, but mstconfig cannot
    write ("Device in Livefish mode is not supported"), so the UEFI ROM can
    never be set in the same pass, and the unlocked MT_2450112034 image
    DEFAULTS EXP_ROM_UEFI_x86_ENABLE to False. 27/27 jumpered runs.
  - No jumper on a locked card: the burn is always refused ("Changing PSID is
    unsupported under controlled FW"). 13/13 unjumpered runs.
The old script checked neither exit status and logged burned=yes / uefi=set
for all of them.
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
    _burns,
    _uefi_sets,
)

# What a jumpered card reports: mstflint still reads the locked OEM image.
_JUMPERED_LOCKED = dict(dev_query=_DEV_QUERY_LOCKED, map_psid="FB_0000000005",
                        livefish=True, fwreset_rc=1)
_UNJUMPERED_LOCKED = dict(dev_query=_DEV_QUERY_LOCKED, map_psid="FB_0000000005")
_FINISHED = dict(dev_query=_DEV_QUERY_WITH_ORIG_PSID,
                 config_query=_CONFIG_QUERY_UEFI_ON)
_UNLOCKED_UEFI_OFF = dict(dev_query=_DEV_QUERY_WITH_ORIG_PSID,
                          config_query=_CONFIG_QUERY_UEFI_OFF)


class Run:
    def __init__(self, tmp, **kw):
        nocards = kw.pop("nocards", False)
        history = kw.pop("history", None)
        d, bindir = _bundle(pathlib.Path(tmp), **kw)
        if nocards:
            (d / "nocards").write_text("")
        logdir = pathlib.Path(tmp) / "logs"
        logdir.mkdir()
        if history:
            (logdir / "20260922T000000Z.log").write_text(history)
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   FAKEDIR=str(d), MEZZ_FLASH_ALL="1", SHUTDOWN_ON_DONE="1",
                   MEZZ_SOL_DEV=str(d / "sol.out"),
                   MEZZ_ISSUE=str(d / "mezz-flash.issue"),
                   MEZZ_STATUS=str(d / "status.json"),
                   MEZZ_LOGDIR=str(logdir))
        self.proc = subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                                   capture_output=True, text=True, env=env,
                                   cwd=str(d))
        self.out = self.proc.stdout
        self.calls = (d / "calls.log").read_text().splitlines()
        sol = d / "sol.out"
        self.sol = sol.read_text() if sol.exists() else ""
        issue = d / "mezz-flash.issue"
        self.issue = issue.read_text() if issue.exists() else ""
        st = d / "status.json"
        self.status = st.read_text() if st.exists() else ""

    @property
    def result(self):
        return [ln for ln in self.out.splitlines() if " RESULT " in ln]

    @property
    def identify(self):
        return [c for c in self.calls if c.startswith("ipmitool") and "identify" in c]

    @property
    def blinking(self):
        return bool(self.identify) and self.identify[-1].endswith("identify force")

    @property
    def powered_off(self):
        return any(c.startswith("shutdown") for c in self.calls)

    def why(self):
        return "stdout:\n%s\ncalls:\n%s" % (self.out, "\n".join(self.calls))


def run(**kw):
    with tempfile.TemporaryDirectory() as tmp:
        return Run(tmp, **kw)


# --- FLIP: remove the jumper ------------------------------------------------

def test_a_jumpered_locked_card_is_burned_but_not_uefi_set():
    r = run(**_JUMPERED_LOCKED)
    assert _burns(r.calls), r.why()
    assert not _uefi_sets(r.calls), (
        "tried an mstconfig set in livefish, which can never work:\n" + r.why())
    assert "bootrom=pending" in r.result[0], r.why()


def test_a_jumpered_burn_ends_dark_and_says_remove_the_jumper():
    r = run(**_JUMPERED_LOCKED)
    assert "verdict=remove-jumper" in r.result[0], r.why()
    assert r.powered_off and not r.blinking, r.why()
    assert "REMOVE THE JUMPER" in r.sol, r.sol


def test_a_jumpered_card_already_carrying_the_new_image_still_says_remove():
    """Livefish reads the flash, so a second jumpered pass sees the target
    image already there. Still not finished: the UEFI ROM is not set."""
    r = run(dev_query=_DEV_QUERY_WITH_ORIG_PSID, livefish=True)
    assert not _burns(r.calls), r.why()
    assert "verdict=remove-jumper" in r.result[0], r.why()
    assert r.powered_off and not r.blinking, r.why()


# --- FLIP: fit the jumper ---------------------------------------------------

def test_a_locked_card_without_the_jumper_is_not_burned():
    """The burn is refused every time (controlled FW); do not pretend."""
    r = run(**_UNJUMPERED_LOCKED)
    assert not _burns(r.calls), r.why()
    assert "verdict=fit-jumper" in r.result[0], r.why()


def test_a_locked_card_without_the_jumper_ends_dark_and_says_fit_it():
    r = run(**_UNJUMPERED_LOCKED)
    assert r.powered_off and not r.blinking, r.why()
    assert "FIT THE JUMPER" in r.sol, r.sol


# --- DONE -------------------------------------------------------------------

def test_setting_the_uefi_rom_without_the_jumper_is_verified_and_done():
    r = run(**_UNLOCKED_UEFI_OFF)
    assert _uefi_sets(r.calls), r.why()
    assert "bootrom=set" in r.result[0] and "verdict=done" in r.result[0], r.why()
    assert r.blinking and r.powered_off, r.why()
    assert "DONE" in r.sol, r.sol


def test_a_finished_card_is_done_blinking_and_stays_up():
    r = run(**_FINISHED)
    assert "verdict=done" in r.result[0], r.why()
    assert r.blinking and not r.powered_off, r.why()


def test_a_stale_blink_from_the_last_run_is_cleared_first():
    """IDENT survives a host power cycle on the BMC. A run that ends dark must
    not leave the previous run's DONE blink showing."""
    r = run(**_UNJUMPERED_LOCKED)
    assert r.identify and r.identify[0].endswith("identify 0"), r.identify


# --- PROBLEM ----------------------------------------------------------------

def test_a_refused_burn_is_a_failure_not_a_burn():
    r = run(burn_refused=True, **_JUMPERED_LOCKED)
    assert "burned=failed" in r.result[0], r.why()
    assert "verdict=problem" in r.result[0], r.why()
    assert not r.powered_off and not r.blinking, r.why()
    assert "PROBLEM" in r.sol, r.sol


def test_a_failed_uefi_write_is_a_problem():
    r = run(uefi_set_fails=True, **_UNLOCKED_UEFI_OFF)
    assert "bootrom=failed" in r.result[0], r.why()
    assert not r.powered_off and not r.blinking, r.why()


def test_a_uefi_write_that_does_not_stick_is_a_problem():
    """mstconfig said OK but Next Boot still reads False: not done."""
    r = run(uefi_set_noop=True, **_UNLOCKED_UEFI_OFF)
    assert "bootrom=failed" in r.result[0], r.why()
    assert "verdict=problem" in r.result[0], r.why()


def test_no_card_at_all_is_a_problem_not_a_done():
    r = run(nocards=True, **_FINISHED)
    assert not r.blinking and not r.powered_off, r.why()
    assert "PROBLEM" in r.sol, r.sol


# --- SOL --------------------------------------------------------------------

def test_the_verdict_is_left_in_the_login_banner():
    """An operator who opens SOL after the run missed the live banner; the
    serial getty's issue file shows it again on Enter."""
    r = run(**_UNJUMPERED_LOCKED)
    assert "FIT THE JUMPER" in r.issue, r.issue
    assert "506b4bf9aabb" in r.issue.lower().replace(":", ""), r.issue


def test_the_banner_carries_earlier_runs_too():
    """With the jumper fitted the BMC is unreachable (NC-SI rides the card),
    so a jumpered run's PROBLEM can only be read on a LATER jumper-less boot.
    The banner therefore lists recent RESULT lines, not just this run's."""
    old = ("5e:00.0: RESULT mac=506b4bf9dead guid=x opn=unknown "
           "psid=FB_0000000005 fw=14.32.1206 sec=[secure-fw] burned=failed "
           "bootaddr=n/a activated=n/a resetrc=n/a bootrom=pending verdict=problem\n")
    r = run(history=old, **_FINISHED)
    row = [ln for ln in r.issue.splitlines() if "506b4bf9dead" in ln]
    assert row, r.issue
    # Values only -- the table's heading row carries the key names.
    assert "problem" in row[0] and "LOCK" in row[0], row[0]


def test_history_from_before_the_fix_is_marked_unverified():
    """Pre-2026-09-22 RESULT lines logged burned=yes / uefi=set without
    checking; an operator must not read them as fact."""
    old = ("5e:00.0: RESULT mac=506b4bf9beef guid=x opn=unknown "
           "psid=MT_2450112034 fw=14.32.1912 sec=[N/A] burned=yes "
           "bootaddr=failed activated=no resetrc=1 uefi=set\n"
           "5e:00.0: RESULT mac=506b4bf9cafe guid=x opn=MCX4411A-ACQ "
           "psid=MT_2450112034 fw=14.32.1912 sec=[N/A] burned=no "
           "bootaddr=n/a activated=n/a resetrc=n/a bootrom=already "
           "romset=none livefish=no verdict=done\n")
    r = run(history=old, **_FINISHED)
    pre = [ln for ln in r.issue.splitlines() if "506b4bf9beef" in ln][0]
    post = [ln for ln in r.issue.splitlines() if "506b4bf9cafe" in ln][0]
    assert "*" in pre, pre
    assert "*" not in post, post
    assert "pre-fix" in r.issue, r.issue


def test_progress_is_on_the_banner_while_a_burn_runs():
    """The station no longer holds boot, so the prompt is up while it works:
    the banner must say what it is doing, and that the card must stay in."""
    with tempfile.TemporaryDirectory() as tmp:
        d, bindir = _bundle(pathlib.Path(tmp), **_JUMPERED_LOCKED)
        issue = d / "mezz-flash.issue"
        snap = d / "issue_at_burn.txt"
        f = bindir / "mstflint"
        f.write_text(f.read_text().replace(
            'for a in "$@"; do [ "$a" = "burn" ] && {',
            'for a in "$@"; do [ "$a" = "burn" ] && {\n'
            '        cp "%s" "%s"' % (issue, snap), 1))
        logdir = pathlib.Path(tmp) / "logs"
        logdir.mkdir()
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   FAKEDIR=str(d), MEZZ_FLASH_ALL="1", SHUTDOWN_ON_DONE="1",
                   MEZZ_SOL_DEV=str(d / "sol.out"), MEZZ_ISSUE=str(issue),
                   MEZZ_LOGDIR=str(logdir))
        subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                       capture_output=True, text=True, env=env, cwd=str(d))
        mid = snap.read_text()
        assert "BURNING" in mid and "DO NOT PULL" in mid, mid
        assert "506b4bf9aabb" in mid.lower().replace(":", ""), mid


# --- banner layout (operator-facing) ---------------------------------------

def _section(issue, start, end=None):
    lines = issue.splitlines()
    i = [n for n, l in enumerate(lines) if start in l][0]
    j = len(lines)
    if end:
        j = [n for n, l in enumerate(lines) if end in l and n > i][0]
    return lines[i:j]


def test_the_current_card_is_one_indented_key_per_line():
    """Operator decision 2026-09-22: the run in progress reads as a list, not
    as one dense key=value row buried among single-line log noise."""
    r = run(**_FINISHED)
    # The card block sits between the history table and the status line.
    cur = "\n".join(_section(r.issue, "Card MAC:", "  DONE  "))
    for key in ("Card MAC:", "Part:", "PSID:", "Firmware:", "Lock:",
                "Jumper:", "Burned:", "Boot ROM:", "Verdict:"):
        line = [l for l in cur.splitlines() if l.strip().startswith(key)]
        assert line, (key, cur)
        assert line[0].startswith("  "), ("key needs indent", line[0])
        value = line[0].split(":", 1)[1]
        assert value.startswith(" ") and value.strip(), ("value needs space", line[0])
    assert "506b4bf9e416" in cur.lower().replace(":", ""), cur


def test_the_status_line_is_last_so_it_cannot_scroll_off():
    """Operator decision 2026-09-22: on a scrolling SOL console the order is
    title, history (oldest first), the card in hand, then the status line."""
    r = run(**_FINISHED)
    assert r.issue.index("earlier runs") < r.issue.index("Card MAC:"), r.issue
    assert r.issue.index("Card MAC:") < r.issue.index("  DONE  "), r.issue


def test_the_history_is_a_table_with_one_heading_row():
    old = ("a: RESULT mac=506b4bf9aaa1 guid=x opn=MCX4411A-ACQ psid=MT_2450112034 "
           "fw=14.32.1912 sec=[N/A] burned=no bootaddr=n/a activated=n/a "
           "resetrc=n/a bootrom=already romset=none livefish=no verdict=done\n")
    r = run(history=old, **_FINISHED)
    hist = _section(r.issue, "earlier runs")
    head = [l for l in hist if "MAC" in l and "VERDICT" in l]
    assert len(head) == 1, hist
    row = [l for l in hist if "506b4bf9aaa1" in l][0]
    assert "mac=" not in row and "verdict=" not in row, ("values only", row)
    assert "done" in row and "MCX4411A-ACQ" in row, row
    assert len(row) <= 80, ("must fit an 80-col SOL screen", len(row), row)


def test_the_banner_table_is_oldest_first():
    import os
    with tempfile.TemporaryDirectory() as tmp:
        d, bindir = _bundle(pathlib.Path(tmp), **_FINISHED)
        logdir = pathlib.Path(tmp) / "logs"
        logdir.mkdir()
        mk = lambda mac: ("a: RESULT mac=%s guid=x opn=MCX4411A-ACQ "
                          "psid=MT_2450112034 fw=14.32.1912 sec=[N/A] burned=no "
                          "bootaddr=n/a activated=n/a resetrc=n/a "
                          "bootrom=already romset=none livefish=no verdict=done\n" % mac)
        (logdir / "20260922T080000Z.log").write_text(mk("506b4bf9old1"))
        (logdir / "20260922T090000Z.log").write_text(mk("506b4bf9new1"))
        os.utime(logdir / "20260922T080000Z.log", (1, 1))
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   FAKEDIR=str(d), MEZZ_FLASH_ALL="1", SHUTDOWN_ON_DONE="1",
                   MEZZ_SOL_DEV=str(d / "sol.out"),
                   MEZZ_ISSUE=str(d / "mezz-flash.issue"),
                   MEZZ_LOGDIR=str(logdir))
        subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                       capture_output=True, text=True, env=env, cwd=str(d))
        issue = (d / "mezz-flash.issue").read_text()
        assert issue.index("506b4bf9old1") < issue.index("506b4bf9new1"), issue


def test_the_prefix_footnote_shows_wherever_the_starred_row_sits():
    """The star can land on any row; the footnote must not depend on it being
    the last one."""
    new = ("a: RESULT mac=506b4bf9new2 guid=x opn=MCX4411A-ACQ psid=MT_2450112034 "
           "fw=14.32.1912 sec=[N/A] burned=no bootaddr=n/a activated=n/a "
           "resetrc=n/a bootrom=already romset=none livefish=no verdict=done\n")
    old = ("a: RESULT mac=506b4bf9old2 guid=x opn=unknown psid=MT_2450112034 "
           "fw=14.32.1912 sec=[N/A] burned=yes bootaddr=failed activated=no "
           "resetrc=1 uefi=set\n")
    # tac puts the LAST line of the log first, so with the pre-fix line FIRST
    # in the file the starred row comes out on top, not at the end -- which is
    # exactly what the old `case "$any" in *"*"` footnote check missed.
    r = run(history=new + old, **_FINISHED)
    rows = [ln for ln in r.issue.splitlines() if "506b4bf9" in ln and "-" in ln]
    assert any(r.rstrip().endswith("*") for r in rows), rows
    assert "pre-fix" in r.issue, r.issue


def _rowfor(mac, verdict="done", bootrom="already", burned="no", fw="14.32.1912"):
    return ("a: RESULT mac=%s guid=x opn=MCX4411A-ACQ psid=MT_2450112034 "
            "fw=%s sec=[N/A] burned=%s bootaddr=n/a activated=n/a resetrc=n/a "
            "bootrom=%s romset=none livefish=no verdict=%s\n"
            % (mac, fw, burned, bootrom, verdict))


def _run_with_logs(logs, **kw):
    """logs: [(stamp, text)] oldest first."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        d, bindir = _bundle(pathlib.Path(tmp), **kw)
        logdir = pathlib.Path(tmp) / "logs"
        logdir.mkdir()
        for i, (stamp, text) in enumerate(logs):
            f = logdir / ("%s.log" % stamp)
            f.write_text(text)
            os.utime(f, (100 + i, 100 + i))
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   FAKEDIR=str(d), MEZZ_FLASH_ALL="1", SHUTDOWN_ON_DONE="1",
                   MEZZ_SOL_DEV=str(d / "sol.out"),
                   MEZZ_ISSUE=str(d / "mezz-flash.issue"),
                   MEZZ_STATUS=str(d / "status.json"), MEZZ_LOGDIR=str(logdir))
        subprocess.run(["bash", str(d / "unlock_mellanox.sh")],
                       capture_output=True, text=True, env=env, cwd=str(d))
        return ((d / "mezz-flash.issue").read_text(),
                (d / "status.json").read_text())


def test_unchanged_repeats_collapse_to_the_most_recent():
    """Operator decision 2026-09-22: five identical rows say nothing five
    times. Keep the newest, count the rest."""
    logs = [("20260922T17%02d00Z" % h, _rowfor("506b4bda9dcc"))
            for h in (10, 20, 30, 40, 50)]
    issue, _ = _run_with_logs(logs, **_FINISHED)
    rows = [l for l in issue.splitlines() if "506b4bda9dcc" in l and "-" in l]
    assert len(rows) == 1, rows
    assert "1750" in rows[0], ("must keep the NEWEST stamp", rows[0])
    assert "x5" in rows[0], ("must say how many runs collapsed", rows[0])
    assert len(rows[0]) <= 80, (len(rows[0]), rows[0])


def test_a_state_change_starts_a_new_row():
    logs = [("20260922T171000Z", _rowfor("506b4bda9dcc", bootrom="set")),
            ("20260922T172000Z", _rowfor("506b4bda9dcc")),
            ("20260922T173000Z", _rowfor("506b4bda9dcc"))]
    issue, _ = _run_with_logs(logs, **_FINISHED)
    rows = [l for l in issue.splitlines() if "506b4bda9dcc" in l and "-" in l]
    assert len(rows) == 2, rows
    assert "1710" in rows[0] and "x2" not in rows[0], rows[0]
    assert "1730" in rows[1] and "x2" in rows[1], rows[1]


def test_different_cards_never_collapse():
    logs = [("20260922T171000Z", _rowfor("506b4bf9aa11")),
            ("20260922T172000Z", _rowfor("506b4bf9bb22"))]
    issue, _ = _run_with_logs(logs, **_FINISHED)
    rows = [l for l in issue.splitlines()
            if ("506b4bf9aa11" in l or "506b4bf9bb22" in l)]
    assert len(rows) == 2, rows


def test_the_json_history_collapses_too_and_counts_runs():
    logs = [("20260922T17%02d00Z" % h, _rowfor("506b4bda9dcc"))
            for h in (10, 20, 30)]
    _, status = _run_with_logs(logs, **_FINISHED)
    import json
    h = json.loads(status)["history"]
    assert len(h) == 1, h
    assert h[0]["runs"] == 3 and h[0]["when"].endswith("173000Z"), h[0]
