#!/usr/bin/env python3
"""post_status.py -- live status of a post.sh run, for SOL and for a tile.

post.sh calls this through post_status.sh's ps_* wrappers at every stage
change. Each call rewrites, atomically:
  /run/flax/post-status.json     the machine-readable state (tile, phase 2)
  /run/issue.d/flax-post.issue   the SOL banner agetty prints above login:
  $logdir/post-status.json       a copy the final rsync carries to the bang

/run/issue.d, NOT /etc/issue.d: the live ISO has no /etc/issue, and its
agetty (util-linux 2.40) only reads /etc/issue.d when /etc/issue exists
(measured on et24b3, 2026-09-25).

A view, never the job: post_status.sh swallows every failure of this script.
Spec: reaper-devel docs/superpowers/specs/2026-09-25-post-sol-status-banner-design.md
"""
import json
import os
import re
import subprocess
import sys
import time

JSON_PATH = os.environ.get("POST_STATUS_JSON", "/run/flax/post-status.json")
ISSUE_PATH = os.environ.get("POST_STATUS_ISSUE", "/run/issue.d/flax-post.issue")
HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
SERIAL_INFO = os.environ.get("POST_SERIAL_INFO", "/proc/tty/driver/serial")

STAGES = {
    "inventory": ["boot", "clock", "ipmi", "biosgate", "nicfw", "biosfw",
                  "inventory", "binrefresh", "tools", "dump", "ident", "poweroff"],
    "memtest": ["boot", "clock", "ipmi", "memtest", "dump", "ident", "poweroff"],
    "postautomate": ["boot", "agent"],
}
# The EXIT hook turns only a RUNNING run into FAILED. A reboot to flash
# (REBOOTING) or a gate power-off (POWER-OFF) also ends in bash's EXIT trap,
# and must keep saying what it is.
HELD = {"REBOOTING", "POWER-OFF", "DONE", "FAILED", "WAITING"}
MARK = {"pending": "[ ]", "running": "[>]", "done": "[x]",
        "skipped": "[-]", "failed": "[!]"}
IPMI = {None: "unknown", 0: "none", 1: "ipmi ok", 2: "ipmi (mono lake, no cfg)"}
WIDTH = 66
CLOCK = "(redrawn \\d \\t UTC -- refreshes every 20s)"


def _now():
    t = os.environ.get("POST_STATUS_NOW")
    return float(t) if t else time.time()


def _mono():
    """Elapsed time runs on the boot clock: ps_init runs BEFORE post.sh's
    chronyd -q steps the wall clock, and a blade RTC can be off by hours or
    years. Tests pin it with POST_STATUS_MONO (or POST_STATUS_NOW)."""
    t = os.environ.get("POST_STATUS_MONO") or os.environ.get("POST_STATUS_NOW")
    return float(t) if t else time.clock_gettime(time.CLOCK_BOOTTIME)


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _hms(secs):
    secs = max(0, int(secs))
    return "%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)


def load():
    try:
        with open(JSON_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _stage(st, name):
    for s in st["stages"]:
        if s["name"] == name:
            return s
    s = {"name": name, "state": "pending", "started": "", "ended": "", "detail": ""}
    st["stages"].append(s)
    return s


def _running(st):
    return [s for s in st["stages"] if s["state"] == "running"]


def _new(action, host, bundle):
    t = _iso(_now())
    st = {"ts": t, "state": "RUNNING", "text": "", "post": True, "action": action,
          "bundle": bundle, "host": host, "mac": "", "ipmigood": None,
          "started": t, "started_mono": _mono(), "elapsed_s": 0,
          "stage": "", "note": "", "logdir": "",
          "stages": [{"name": n, "state": "pending", "started": "", "ended": "",
                      "detail": ""}
                     for n in STAGES.get(action, STAGES["inventory"])]}
    boot = _stage(st, "boot")
    boot.update(state="done", started=t, ended=t, detail=action)
    return st


def _auto_text(st):
    if st["state"] != "RUNNING" or not st["stage"]:
        return st["text"]
    what = st["note"] or "working"
    return "%s: %s -- do not pull the blade." % (st["stage"], what)


def render(st, issue=False):
    """The banner. issue=True: the agetty issue-file form -- dynamic text has
    its backslashes neutralised (agetty expands \\d, \\t, \\n ...) and the
    clock line is added, which is what makes every reload actually repaint."""
    def clean(s):
        return str(s).replace("\\", "/") if issue else str(s)

    title = " FLAX POST -- %s " % clean(st["action"]).upper()
    pad = max(0, WIDTH - len(title))
    out = ["=" * (pad // 2) + title + "=" * (pad - pad // 2)]
    # A fixed grid: every cell as wide as the longest "[x] name" plus a gap,
    # so the marks line up in columns on every row (operator, 2026-09-25).
    cells = ["%s %s" % (MARK.get(s["state"], "[?]"), clean(s["name"])) for s in st["stages"]]
    width = max(len(c) for c in cells) + 2 if cells else 1
    per_row = max(1, (WIDTH - 2) // width)
    for i in range(0, len(cells), per_row):
        out.append(("  " + "".join(c.ljust(width) for c in cells[i:i + per_row])).rstrip())
    out.append("")
    out.append("  Node:      %s   MAC %s" % (clean(st["host"] or "?"), clean(st["mac"] or "?")))
    out.append("  BMC:       %s" % IPMI.get(st["ipmigood"], clean(st["ipmigood"])))
    stage = clean(st["stage"]) + (" -- " + clean(st["note"]) if st["note"] else "")
    out.append("  Stage:     %s" % (stage or "-"))
    mono = _mono()
    elapsed = "  Elapsed:   %s" % _hms(mono - st.get("started_mono", mono))
    run = _running(st)
    if run and "started_mono" in run[0]:
        elapsed += "  (stage %s)" % _hms(mono - run[0]["started_mono"])
    out.append(elapsed)
    out.append("  Bundle:    %s" % clean(st["bundle"]))
    out.append("  Logged in? %s/poststate -w" % HOOK_DIR)
    out.append("")
    out.append("%s  %s  %s" % (st["ts"], st["state"], clean(st["text"]).replace("\n", " ")))
    if issue:
        out.append(CLOCK)
    out.append("=" * WIDTH)
    return "\n".join(out) + "\n"


def _write(path, text):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        pass            # each surface fails alone; the others still land


def _logdir_blob(st, blob):
    """The logdir copy is what the final rsync carries to
    /export/nodes/post-<mac>/<stamp>/. The last one written before the rsync
    is `begin dump`'s, so verbatim it would say RUNNING/dump on every
    finished run. Once the dump is under way it says DELIVERED instead: the
    results reached the bang, and the node went on to ident + power off."""
    if st["stage"] != "dump" or not any(
            s["name"] == "dump" and s["state"] == "running" for s in st["stages"]):
        return blob
    shipped = dict(st, state="DELIVERED",
                   text="INVENTORY COLLECTED at %s -- not a pass: CHECK THE GUI for "
                        "population and errors. The node then powers off." % st["ts"])
    return json.dumps(shipped, indent=None, separators=(",", ":")) + "\n"


def save(st):
    now = _now()
    st["ts"] = _iso(now)
    st["elapsed_s"] = max(0, int(_mono() - st.get("started_mono", _mono())))
    st["text"] = _auto_text(st)
    blob = json.dumps(st, indent=None, separators=(",", ":")) + "\n"
    _write(JSON_PATH, blob)
    if st.get("logdir"):
        _write(os.path.join(st["logdir"], "post-status.json"), _logdir_blob(st, blob))
    _write(ISSUE_PATH, render(st, issue=True))


def _finish_stage(st, state, detail):
    run = _running(st)
    if run:
        run[0].update(state=state, ended=_iso(_now()))
        if detail:
            run[0]["detail"] = detail
    st["note"] = ""


def _tx(n):
    try:
        with open(SERIAL_INFO) as f:
            for line in f:
                if line.startswith("%s:" % n):
                    m = re.search(r" tx:(\d+)", line)
                    return int(m.group(1)) if m else None
    except OSError:
        pass
    return None


def flush(tty):
    """Hold the power-off until the final frame has left the SOL UART.

    Baseline the port's tx counter, trigger the repaint, then wait for tx to
    move (agetty writing) and hold still for SETTLE (the UART drained). A
    sleep only guessed: on et24b3 the power cut beat the repaint and the last
    frame SOL ever showed was "RUNNING dump". Capped at POST_FLUSH_MAX (5s),
    so a stalled port -- or a logged-in operator, where no getty repaints --
    never holds the power-off for long."""
    n = tty[4:] if tty.startswith("ttyS") else ""
    cap = float(os.environ.get("POST_FLUSH_MAX", "5"))
    settle, poll = 0.5, 0.1
    start = time.monotonic()
    last = _tx(n)
    if last is None:
        return 0
    try:
        subprocess.run([os.environ.get("POST_AGETTY", "agetty"), "--reload"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass
    moved, still_since = False, time.monotonic()
    while time.monotonic() - start < cap:
        time.sleep(poll)
        cur = _tx(n)
        if cur is None:
            return 0
        if cur != last:
            moved, last, still_since = True, cur, time.monotonic()
        elif moved and time.monotonic() - still_since >= settle:
            return 0
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "show":
        watch = "-w" in args
        while True:
            st = load()
            text = render(st) if st else "no post status yet (%s)\n" % JSON_PATH
            if watch:
                sys.stdout.write("\033[H\033[2J")
            sys.stdout.write(text)
            sys.stdout.flush()
            if not watch:
                return 0
            time.sleep(5)
    if cmd == "flush":
        return flush(args[0] if args else "")
    if cmd == "init":
        save(_new(args[0], args[1], args[2]))
        return 0
    st = load()
    if st is None:
        return 0                       # never initialised: nothing to update
    t = _iso(_now())
    if cmd == "begin":
        for s in _running(st):
            if s["name"] != args[0]:
                s.update(state="done", ended=t)
        s = _stage(st, args[0])
        s.update(state="running", started=t, ended="", started_mono=_mono())
        st["stage"] = args[0]
        st["note"] = args[1] if len(args) > 1 else ""
        st["state"] = "RUNNING"
    elif cmd == "note":
        st["note"] = args[0] if args else ""
    elif cmd == "done":
        _finish_stage(st, "done", args[0] if args else "")
    elif cmd == "fail":
        _finish_stage(st, "failed", args[0] if args else "")
    elif cmd == "skip":
        s = _stage(st, args[0])
        s.update(state="skipped", detail=args[1] if len(args) > 1 else "")
    elif cmd == "state":
        st["state"], st["text"] = args[0], (args[1] if len(args) > 1 else "")
    elif cmd == "set":
        key, val = args[0], args[1]
        if key == "ipmigood":
            try:
                val = int(val)
            except ValueError:
                val = None
        if key in ("mac", "ipmigood", "host", "logdir"):
            st[key] = val
    elif cmd == "finish":
        for s in st["stages"]:
            if s["state"] == "pending":
                s["state"] = "skipped"
        st["state"], st["text"] = args[0], (args[1] if len(args) > 1 else "")
    elif cmd == "exit":
        if st["state"] in HELD:
            return 0
        rc = args[0] if args else "?"
        system = args[1] if len(args) > 1 else ""
        cur = st["stage"] or "?"
        if system == "stopping":
            # A flash script queued `shutdown -r now` and returned; post.sh
            # got into a later stage before systemd's SIGTERM arrived.
            st["state"] = "REBOOTING"
            st["text"] = "system shutting down during %s -- not a crash." % cur
        else:
            # rc can read 0 when bash dies waiting on a child; name the stage.
            _finish_stage(st, "failed", "post.sh exited rc %s" % rc)
            st["state"] = "FAILED"
            st["text"] = "post.sh stopped (rc %s) during %s -- see banghook journal." % (rc, cur)
    else:
        print("unknown command: %s" % cmd, file=sys.stderr)
        return 2
    save(st)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
