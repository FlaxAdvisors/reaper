"""post_status.py -- the state and rendering behind the post SOL banner.

Spec: docs/superpowers/specs/2026-09-25-post-sol-status-banner-design.md
"""
import json
import os
import pathlib
import subprocess

import pytest

import pathlib as _pl
REAPER = _pl.Path(__file__).resolve().parents[1]

POST = REAPER / "roles" / "apply_pxe_payloads" / "files" / "post"
PY = POST / "post_status.py"



class St:
    def __init__(self, tmp):
        self.json = tmp / "run" / "flax" / "post-status.json"
        self.issue = tmp / "run" / "issue.d" / "flax-post.issue"
        self.now = 1790300000
        self.mono = None

    def __call__(self, *args, now=None, mono=None):
        if now is not None:
            self.now = now
        if mono is not None:
            self.mono = mono
        env = dict(os.environ, POST_STATUS_JSON=str(self.json),
                   POST_STATUS_ISSUE=str(self.issue), POST_STATUS_NOW=str(self.now))
        if self.mono is not None:
            env["POST_STATUS_MONO"] = str(self.mono)
        r = subprocess.run(["python3", str(PY)] + [str(a) for a in args],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout

    @property
    def data(self):
        return json.loads(self.json.read_text())

    def stage(self, name):
        return next(s for s in self.data["stages"] if s["name"] == name)


@pytest.fixture
def st(tmp_path):
    s = St(tmp_path)
    s("init", "inventory", "leopard9b1", "abc1234@20260925T000000Z")
    return s


def test_init_lays_out_the_inventory_stages_in_order(st):
    names = [s["name"] for s in st.data["stages"]]
    assert names == ["boot", "clock", "ipmi", "diskwipe", "biosgate", "nicfw", "biosfw",
                     "inventory", "binrefresh", "tools", "tpmclear", "dump", "ident", "poweroff"]
    assert st.stage("boot")["state"] == "done"
    assert st.data["state"] == "RUNNING" and st.data["post"] is True
    assert st.data["bundle"] == "abc1234@20260925T000000Z"


def test_postautomate_has_only_boot_and_agent(tmp_path):
    s = St(tmp_path)
    s("init", "postautomate", "tp1", "x")
    assert [x["name"] for x in s.data["stages"]] == ["boot", "agent"]


def test_memtest_has_its_own_stage_list(tmp_path):
    s = St(tmp_path)
    s("init", "memtest", "tp1", "x")
    assert [x["name"] for x in s.data["stages"]] == \
        ["boot", "clock", "ipmi", "memtest", "dump", "ident", "poweroff"]


def test_begin_done_note_and_marks(st):
    st("begin", "clock", "chronyd + hwclock")
    assert st.stage("clock")["state"] == "running"
    st("done")
    st("begin", "inventory", "hwinfo (3 of 6)")
    st("skip", "biosgate", "not Tioga Pass")
    out = st("show")
    assert "[x] clock" in out and "[>] inventory" in out
    assert "[-] biosgate" in out and "[ ] dump" in out
    assert "Stage:     inventory -- hwinfo (3 of 6)" in out


def test_begin_closes_a_stage_left_running(st):
    st("begin", "clock")
    st("begin", "ipmi")
    assert st.stage("clock")["state"] == "done"


def test_status_line_is_last_before_the_rule(st):
    st("begin", "inventory", "lspci")
    lines = st.issue.read_text().rstrip("\n").splitlines()
    assert lines[-1].startswith("=====")
    assert lines[-2].startswith("(redrawn \\d \\t UTC")        # clock line, issue only
    assert "  RUNNING  inventory: lspci -- do not pull the blade." in lines[-3]


def test_show_has_no_clock_line(st):
    assert "\\d" not in st("show")


def test_elapsed_counts_from_init(st):
    st("begin", "clock", now=1790300252)
    assert st.data["elapsed_s"] == 252
    assert "Elapsed:   00:04:12" in st("show")


def test_state_rebooting_then_next_begin_is_running_again(st):
    st("begin", "biosfw")
    st("state", "REBOOTING", "biosfw: reboots if it flashes the BIOS -- not a crash.")
    assert st.data["state"] == "REBOOTING"
    st("done")
    st("begin", "inventory")
    assert st.data["state"] == "RUNNING"


def test_finish_skips_what_never_ran(st):
    st("finish", "WAITING", "live boot tasks done -- up for ssh.")
    d = st.data
    assert d["state"] == "WAITING"
    assert st.stage("dump")["state"] == "skipped"
    assert st.stage("boot")["state"] == "done"


def test_exit_while_running_fails_the_running_stage(st):
    st("begin", "tools", "dimmerr")
    st("exit", 3)
    assert st.data["state"] == "FAILED"
    assert st.stage("tools")["state"] == "failed"
    assert "rc 3" in st.data["text"]


@pytest.mark.parametrize("held", ["REBOOTING", "POWER-OFF", "WAITING", "DONE", "FAILED"])
def test_exit_never_overwrites_a_held_state(st, held):
    st("begin", "biosfw")
    st("state", held, "x")
    st("exit", 143)
    assert st.data["state"] == held


def test_set_fields(st):
    st("set", "mac", "506b4bf9e84e")
    st("set", "ipmigood", "1")
    d = st.data
    assert d["mac"] == "506b4bf9e84e" and d["ipmigood"] == 1
    assert "MAC 506b4bf9e84e" in st("show") and "ipmi ok" in st("show")


def test_logdir_gets_a_copy_on_every_write(st, tmp_path):
    logdir = tmp_path / "post-x" / "20260925_010101"
    logdir.mkdir(parents=True)
    st("set", "logdir", logdir)
    st("begin", "dump")
    copy = json.loads((logdir / "post-status.json").read_text())
    assert copy["stage"] == "dump"


def test_quotes_and_backslashes_stay_valid_json(st):
    nasty = 'say "hi" \\ back\\slash\nnewline \\d'
    st("note", nasty)
    assert st.data["note"] == nasty                 # parsed back exactly


def test_issue_file_neutralises_backslashes(st):
    # agetty expands \d, \t, \n ... in the issue file; only OUR clock line may
    # carry escapes, dynamic text must not inject any.
    st("begin", "inventory", "bad \\d \\n note")
    body = st.issue.read_text()
    assert "bad \\d" not in body
    assert body.count("\\") == 2                     # just the clock line's \d \t


def test_writes_are_atomic_no_tmp_left(st):
    st("begin", "clock")
    leftovers = [p.name for p in st.json.parent.iterdir() if p.name.endswith(".tmp")]
    leftovers += [p.name for p in st.issue.parent.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, leftovers


def test_an_unwritable_issue_dir_still_writes_the_json(tmp_path):
    s = St(tmp_path)
    s.issue = pathlib.Path("/proc/nope/flax-post.issue")    # cannot be created
    s("init", "inventory", "h", "b")
    s("begin", "clock")
    assert s.data["stage"] == "clock"


def test_show_without_state_says_so(tmp_path):
    s = St(tmp_path)
    assert "no post status yet" in s("show")


def test_stage_cells_never_run_together(st):
    # Found rendering the sample banner: "[>] inventory[ ] binrefresh" --
    # a 9-letter stage name exactly filled a 13-column cell.
    import re
    st("begin", "inventory")
    st("skip", "binrefresh", "x")
    rows = [l for l in st("show").splitlines() if re.search(r"\[[ x>!-]\]", l)]
    for row in rows:
        assert not re.search(r"\S\[[ x>!-]\]", row), row


def test_elapsed_survives_the_ntp_clock_step(tmp_path):
    # Review #2: ps_init runs before chronyd -q steps the clock. A blade RTC
    # off by a year must not make Elapsed read a year -- use the boot clock.
    s = St(tmp_path)
    s("init", "inventory", "h", "b", now=1790300000, mono=50)
    s("begin", "clock", now=1790300000 - 86400 * 365, mono=80)
    assert s.data["elapsed_s"] == 30
    out = s("show")
    assert "Elapsed:   00:00:30  (stage 00:00:00)" in out, out
    s("begin", "ipmi", now=1790300000 - 86400 * 365 + 40, mono=120)
    assert "Elapsed:   00:01:10  (stage 00:00:00)" in s("show")


def test_the_copy_the_dump_delivers_does_not_look_hung(st, tmp_path):
    # Review #3: the final rsync ships the logdir copy written by `begin dump`,
    # so /export/nodes/post-<mac>/latest/ always said RUNNING/dump. That copy
    # now says DELIVERED; the node's own /run copy keeps the live truth.
    logdir = tmp_path / "post-x" / "20260925_010101"
    logdir.mkdir(parents=True)
    st("set", "logdir", logdir)
    st("begin", "dump", "rsync to bang:/export/nodes")
    shipped = json.loads((logdir / "post-status.json").read_text())
    assert shipped["state"] == "DELIVERED", shipped
    assert "powers off" in shipped["text"] or "powered off" in shipped["text"], shipped["text"]
    assert st.data["state"] == "RUNNING"                  # live copy unchanged


def test_logdir_copy_before_the_dump_is_a_plain_copy(st, tmp_path):
    logdir = tmp_path / "post-y" / "20260925_010101"
    logdir.mkdir(parents=True)
    st("set", "logdir", logdir)
    st("begin", "tools", "dimmerr")
    assert json.loads((logdir / "post-status.json").read_text())["state"] == "RUNNING"


def test_the_delivered_copy_says_check_the_gui(st, tmp_path):
    logdir = tmp_path / "post-z" / "20260925_010101"
    logdir.mkdir(parents=True)
    st("set", "logdir", logdir)
    st("begin", "dump")
    text = json.loads((logdir / "post-status.json").read_text())["text"]
    assert "CHECK THE GUI" in text and "not a pass" in text, text


def _flush(tmp_path, info_text, **env):
    info = tmp_path / "serial"
    if info_text is not None:
        info.write_text(info_text)
    e = dict(os.environ, POST_SERIAL_INFO=str(info), POST_AGETTY="true", **env)
    import time
    t0 = time.monotonic()
    r = subprocess.run(["python3", str(PY), "flush", "ttyS1"], capture_output=True,
                       text=True, env=e)
    return r, time.monotonic() - t0


def test_flush_gives_up_at_the_cap_on_a_port_that_never_moves(tmp_path):
    # A stalled UART must never hold the power-off hostage.
    r, took = _flush(tmp_path, "serinfo:1.0\n1: uart:16550A port:000002F8 irq:3 tx:500 rx:0\n",
                     POST_FLUSH_MAX="0.6")
    assert r.returncode == 0, r.stderr
    assert 0.5 <= took < 2.0, took


def test_flush_returns_at_once_without_serial_info(tmp_path):
    r, took = _flush(tmp_path, None, POST_FLUSH_MAX="5")
    assert r.returncode == 0 and took < 1.0, (r.stderr, took)


@pytest.mark.serial   # wall-clock window: a busy box starts the flush late (seen at -n auto)
def test_flush_waits_for_tx_to_move_then_settle(tmp_path):
    # A writer bumps tx for ~0.8s (the repaint going out), then stops; flush
    # must return after it settles, well before the cap.
    import threading, time
    info = tmp_path / "serial"
    info.write_text("serinfo:1.0\n1: uart:16550A port:000002F8 irq:3 tx:100 rx:0\n")

    def writer():
        time.sleep(0.2)
        for i in range(8):
            info.write_text("serinfo:1.0\n1: uart:16550A port:000002F8 irq:3 tx:%d rx:0\n"
                            % (200 + i * 100))
            time.sleep(0.1)
    th = threading.Thread(target=writer)
    th.start()
    r, took = _flush(tmp_path, None, POST_FLUSH_MAX="6")
    th.join()
    assert r.returncode == 0, r.stderr
    assert 1.0 <= took < 3.5, took


def test_checkbox_columns_line_up(st):
    # Operator, 2026-09-25: "[>] inventory [ ] binrefresh" pushed its row out
    # of line. Every row's marks must sit in the same columns.
    import re
    st("begin", "inventory")
    rows = [l for l in st("show").splitlines() if re.search(r"\[[ x>!-]\] ", l)]
    cols = [[m.start() for m in re.finditer(r"\[[ x>!-]\] ", r)] for r in rows]
    assert len(rows) >= 2, rows
    for c in cols[1:]:
        assert c == cols[0][:len(c)], "\n".join(rows)
    assert max(len(r) for r in rows) <= 78, rows          # fits an 80-col SOL


def test_bmc_wait_shows_on_the_bmc_line(tmp_path, monkeypatch):
    # bmc_ready.sh: a gate waiting for a rebooting BMC says so on the banner's
    # BMC line, and clearing it restores the plain line.
    import importlib.util
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "roles" / \
        "apply_pxe_payloads" / "files" / "post" / "post_status.py"
    spec = importlib.util.spec_from_file_location("post_status_bmc", src)
    ps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ps)
    monkeypatch.setattr(ps, "JSON_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(ps, "ISSUE_PATH", str(tmp_path / "issue"))
    ps.main(["init", "inventory", "tp6b1", "b@1"])
    ps.main(["set", "ipmigood", "1"])
    ps.main(["set", "bmc", "not answering (45s)"])
    assert "  BMC:       ipmi ok -- not answering (45s)" in ps.render(ps.load())
    ps.main(["set", "bmc", ""])
    assert "  BMC:       ipmi ok\n" in ps.render(ps.load())
