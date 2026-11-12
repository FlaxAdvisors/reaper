"""post_status.sh -- the bash side of the post SOL banner.

Runs the REAL post_status.sh/.py/poststate plus the station's
serial_console_fixup.sh + serial_watchdog.sh, laid out like the extracted
post.tgz in /opt/flax/hook, with fake agetty/systemd-run/systemctl/setserial.
"""
import json
import os
import shutil
import subprocess


import pathlib as _pl
REAPER = _pl.Path(__file__).resolve().parents[1]
PAYLOAD = REAPER / "scripts" / "unlock-with-ubuntu" / "payload"

POST = REAPER / "roles" / "apply_pxe_payloads" / "files" / "post"

FAKES = ("agetty", "systemd-run", "systemctl", "setserial")


class Hook:
    def __init__(self, tmp, console="tty0 ttyS1\n", uart="16550A"):
        self.tmp = tmp
        self.dir = tmp / "hook"
        self.dir.mkdir()
        for f in ("post_status.sh", "post_status.py", "poststate"):
            shutil.copy(POST / f, self.dir / f)
        for f in ("serial_console_fixup.sh", "serial_watchdog.sh"):
            shutil.copy(PAYLOAD / f, self.dir / f)
        (self.dir / "BUILD").write_text("abc1234@20260925T000000Z\n")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.calls = tmp / "calls.log"
        for name in FAKES:
            p = self.bin / name
            p.write_text('#!/bin/bash\necho "%s $*" >> "%s"\nexit 0\n' % (name, self.calls))
            p.chmod(0o755)
        (self.dir / "setserial").write_text(
            '#!/bin/bash\necho "hook-setserial $*" >> "%s"\n' % self.calls)
        (self.dir / "setserial").chmod(0o755)
        self.active = tmp / "console_active"
        self.active.write_text(console)
        self.serial = tmp / "serial"
        self.serial.write_text("serinfo:1.0\n0: uart:16550A port:000003F8 irq:4 tx:1 rx:0\n"
                               "1: uart:%s port:000002F8 irq:3 tx:5 rx:0\n" % uart)
        self.json = tmp / "run" / "flax" / "post-status.json"
        self.issue = tmp / "run" / "issue.d" / "flax-post.issue"

    def env(self, **extra):
        e = dict(os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"]),
                 POST_STATUS_JSON=str(self.json), POST_STATUS_ISSUE=str(self.issue),
                 POST_CONSOLE_ACTIVE=str(self.active),
                 MEZZ_SERIAL_INFO=str(self.serial), MEZZ_CONSOLE_ACTIVE=str(self.active),
                 MEZZ_TX_SETTLE="0", MEZZ_TTY_DEV=str(self.tmp / "ttydev"))
        e.update(extra)
        return e

    def run(self, body, **extra):
        script = '. "%s/post_status.sh"\n%s\n' % (self.dir, body)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=self.env(**extra), cwd=str(self.dir))

    def calls_of(self, name):
        if not self.calls.exists():
            return []
        return [l for l in self.calls.read_text().splitlines() if l.split(" ", 1)[0] == name]

    @property
    def data(self):
        return json.loads(self.json.read_text())


def test_init_writes_json_and_issue(tmp_path):
    h = Hook(tmp_path)
    r = h.run("ps_init inventory")
    assert r.returncode == 0, r.stderr
    assert h.data["action"] == "inventory"
    assert h.data["bundle"] == "abc1234@20260925T000000Z"
    assert "FLAX POST -- INVENTORY" in h.issue.read_text()


def test_serial_console_starts_both_timers_on_that_tty(tmp_path):
    h = Hook(tmp_path)
    h.run("ps_init inventory")
    runs = h.calls_of("systemd-run")
    assert any("--unit=flax-post-banner" in c and "agetty --reload" in c for c in runs), runs
    wd = [c for c in runs if "--unit=flax-post-watchdog" in c]
    assert wd and wd[0].endswith("serial_watchdog.sh ttyS1"), runs
    assert "MEZZ_WD_STATE=/run/flax/post-wd" in wd[0]


def test_every_call_reloads_agetty_when_serial(tmp_path):
    h = Hook(tmp_path)
    h.run("ps_init inventory; ps_begin clock; ps_done")
    assert len(h.calls_of("agetty")) >= 3


def test_no_serial_console_starts_nothing(tmp_path):
    h = Hook(tmp_path, console="tty0\n")
    h.run("ps_init inventory; ps_begin clock; ps_done")
    assert not h.calls_of("systemd-run")
    assert not h.calls_of("agetty")
    # no fixup either (its `systemctl --no-block start serial-getty@...`); the
    # EXIT trap's read-only `is-system-running` query is fine.
    assert not [c for c in h.calls_of("systemctl") if "is-system-running" not in c]
    assert h.data["stage"] == "clock"             # JSON still written


def test_an_undetected_uart_is_reprobed_with_the_shipped_setserial(tmp_path):
    h = Hook(tmp_path, uart="unknown")
    h.run("ps_init inventory")
    probes = h.calls_of("hook-setserial")
    assert probes and "/dev/ttyS1 port 0x2f8 irq 3 autoconfig auto_irq" in probes[0], \
        h.calls.read_text()


def test_ps_calls_preserve_callers_status(tmp_path):
    h = Hook(tmp_path)
    r = h.run("ps_init inventory\nfalse; ps_note x; echo rc=$?\n"
              "(exit 7); ps_begin clock; echo rc=$?\ntrue; ps_done; echo rc=$?")
    assert r.stdout.split() == ["rc=1", "rc=7", "rc=0"], r.stdout + r.stderr


def test_no_python_means_no_status_and_no_error(tmp_path):
    h = Hook(tmp_path)
    r = h.run("ps_init inventory; ps_begin clock; false; ps_done; echo rc=$?",
              POST_STATUS_PYTHON="/nonexistent/python3")
    assert "rc=1" in r.stdout and r.stderr == "", r.stderr
    assert not h.json.exists()


def test_unwritable_status_path_is_harmless(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        h = Hook(tmp_path)
        r = h.run("ps_init inventory; ps_begin clock; ps_done; echo rc=$?",
                  POST_STATUS_JSON=str(ro / "sub" / "x.json"))
        assert r.stdout.strip() == "rc=0" and r.returncode == 0, r.stderr
    finally:
        ro.chmod(0o755)


def test_exit_mid_stage_marks_failed(tmp_path):
    h = Hook(tmp_path)
    r = h.run("ps_init inventory; ps_begin tools dimmerr; exit 3")
    assert r.returncode == 3
    d = h.data
    assert d["state"] == "FAILED"
    assert next(s for s in d["stages"] if s["name"] == "tools")["state"] == "failed"


def test_exit_while_rebooting_keeps_rebooting(tmp_path):
    h = Hook(tmp_path)
    h.run("ps_init inventory; ps_begin biosfw\n"
          "ps_state REBOOTING 'biosfw: reboots if it flashes the BIOS -- not a crash.'\n"
          "exit 143")
    assert h.data["state"] == "REBOOTING"


def test_finish_then_exit_keeps_the_verdict(tmp_path):
    h = Hook(tmp_path)
    h.run("ps_init liveboot; ps_finish WAITING 'up for ssh'; exit 0")
    assert h.data["state"] == "WAITING"


def test_poststate_prints_the_banner(tmp_path):
    h = Hook(tmp_path)
    # Inside the same shell: once that shell exits unfinished, the EXIT trap
    # rightly turns the run FAILED, which is not what this test is about.
    r = h.run('ps_init inventory; ps_begin inventory hwinfo; "%s/poststate"' % h.dir)
    assert r.returncode == 0 and "[>] inventory" in r.stdout, r.stderr
    assert "Stage:     inventory -- hwinfo" in r.stdout


def test_a_reboot_queued_by_a_flash_then_killing_a_later_stage_is_not_failed(tmp_path):
    # Review #1: update_quanta_leopard.sh ends in `shutdown -r now`, which
    # returns at once. post.sh carries on (biosfw done, inventory RUNNING),
    # then systemd's SIGTERM kills banghook -- the trap must see the system
    # going down and say so, not FAILED.
    h = Hook(tmp_path)
    (h.bin / "systemctl").write_text(
        '#!/bin/bash\necho "systemctl $*" >> "%s"\n'
        '[ "$1" = "is-system-running" ] && { echo stopping; exit 1; }\nexit 0\n' % h.calls)
    h.run("ps_init inventory; ps_begin biosfw\n"
          "ps_state REBOOTING 'biosfw: reboots if it flashes the BIOS -- not a crash.'\n"
          "ps_done; ps_begin inventory hwinfo; exit 143")
    d = h.data
    assert d["state"] == "REBOOTING", d
    assert "shutting down" in d["text"] and "inventory" in d["text"], d["text"]
    assert next(s for s in d["stages"] if s["name"] == "inventory")["state"] != "failed"


def test_a_real_crash_while_the_system_runs_is_still_failed(tmp_path):
    h = Hook(tmp_path)
    (h.bin / "systemctl").write_text(
        '#!/bin/bash\n[ "$1" = "is-system-running" ] && { echo running; exit 0; }\nexit 0\n')
    h.run("ps_init inventory; ps_begin inventory hwinfo; exit 143")
    assert h.data["state"] == "FAILED"


def test_a_second_run_in_the_same_boot_skips_fixup_and_timers(tmp_path):
    # Review #5: `post.sh postautomate` is launched over ssh after a staylive
    # boot, inside host_qual's 30s budget. The first run already re-probed
    # the UART and started both timers; /run is tmpfs, so a status file
    # there means "this boot, already done" -- don't pay ~6s again.
    h = Hook(tmp_path, uart="unknown")
    h.json.parent.mkdir(parents=True)
    h.json.write_text('{"state":"WAITING"}\n')
    h.run("ps_init postautomate; ps_begin agent flax-qual-agent")
    assert not h.calls_of("hook-setserial"), h.calls.read_text()
    assert not h.calls_of("systemd-run"), h.calls.read_text()
    assert h.calls_of("agetty")                      # still redraws
    assert h.data["action"] == "postautomate"        # and re-inits the state


def test_ps_flush_waits_on_the_serial_port_then_syncs(tmp_path):
    h = Hook(tmp_path)
    (h.bin / "sync").write_text('#!/bin/bash\necho "sync $*" >> "%s"\n' % h.calls)
    (h.bin / "sync").chmod(0o755)
    r = h.run("ps_init inventory; false; ps_flush; echo rc=$?",
              POST_SERIAL_INFO=str(h.serial), POST_FLUSH_MAX="0.3")
    assert "rc=1" in r.stdout, r.stdout + r.stderr      # caller's $? kept
    assert h.calls_of("sync"), h.calls.read_text()


def test_ps_flush_without_serial_only_syncs(tmp_path):
    h = Hook(tmp_path, console="tty0\n")
    (h.bin / "sync").write_text('#!/bin/bash\necho "sync $*" >> "%s"\n' % h.calls)
    (h.bin / "sync").chmod(0o755)
    import time
    t0 = time.monotonic()
    h.run("ps_init inventory; ps_flush", POST_FLUSH_MAX="5")
    assert time.monotonic() - t0 < 3
    assert h.calls_of("sync")
