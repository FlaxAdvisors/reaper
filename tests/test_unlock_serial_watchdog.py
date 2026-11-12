"""A stalled SOL port is un-stuck by restarting its login prompt.

et9b1, 2026-09-22 17:5x: the SOL UART stopped transmitting mid-run --
tx frozen at 64925 across 25s while the banner rewrote every 20s, rx:0, and
the operator saw nothing in the triage UI. `setserial ... autoconfig` is
refused while a getty holds the port ("Device or resource busy"), but
`systemctl restart serial-getty@ttyS1` moved tx again at once (64925 ->
66422) and a full banner reached SOL.

So: watch the tx counter, and when it stops moving while the banner is still
being written, restart the getty. Runtime only -- if the port stalls during
boot, PID1 is stuck writing to the console and no timer runs; a BMC cold
reset is the cure for that one.
"""
import os
import subprocess
import tempfile

_PAYLOAD = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "unlock-with-ubuntu", "payload")
_WD = os.path.join(_PAYLOAD, "serial_watchdog.sh")


def _info(tx):
    return ("serinfo:1.0 driver revision:\n"
            "0: uart:16550A port:000003F8 irq:4 tx:0 rx:0 CTS|DSR|CD\n"
            "1: uart:16550A port:000002F8 irq:3 tx:%d rx:0 RTS|CTS|DTR|DSR|CD\n"
            % tx)


class Station:
    """A fake station: /proc serial info, console=, a clock, fake systemctl."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.bin = os.path.join(tmp, "bin")
        os.mkdir(self.bin)
        self.calls = os.path.join(tmp, "calls.log")
        # `who` is faked in every test: restarting a getty would kill a
        # logged-in operator, so the watchdog must check for a session first.
        self.who = os.path.join(tmp, "who.out")
        open(self.who, "w").close()
        p = os.path.join(self.bin, "who")
        with open(p, "w") as f:
            f.write('#!/bin/bash\ncat "%s"\n' % self.who)
        os.chmod(p, 0o755)
        for name in ("systemctl",):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write('#!/bin/bash\necho "%s $*" >> "%s"\n'
                        '[ "$1" = "is-active" ] && { cat "%s/active"; exit 0; }\n'
                        'exit 0\n' % (name, self.calls, tmp))
            os.chmod(p, 0o755)
        self.write_active("active")
        self.info = os.path.join(tmp, "serial")
        self.state = os.path.join(tmp, "state")
        self.console = os.path.join(tmp, "console_active")
        with open(self.console, "w") as f:
            f.write("tty0 ttyS1\n")

    def login(self, tty):
        with open(self.who, "w") as f:
            f.write("flax     %s        2026-09-22 19:20\n" % tty)

    def write_active(self, what):
        with open(os.path.join(self.tmp, "active"), "w") as f:
            f.write(what + "\n")

    def tick(self, tx, now=None):
        with open(self.info, "w") as f:
            f.write(_info(tx))
        env = dict(os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"]),
                   MEZZ_SERIAL_INFO=self.info, MEZZ_CONSOLE_ACTIVE=self.console,
                   MEZZ_WD_STATE=self.state)
        if now is not None:
            env["MEZZ_WD_NOW"] = str(now)
        return subprocess.run(["bash", _WD], capture_output=True, text=True,
                              env=env)

    def restarts(self):
        if not os.path.exists(self.calls):
            return []
        return [c for c in open(self.calls).read().splitlines()
                if c.startswith("systemctl restart")]


def test_a_moving_port_is_left_alone():
    with tempfile.TemporaryDirectory() as tmp:
        s = Station(tmp)
        s.tick(100, now=1000)
        s.tick(200, now=1030)
        s.tick(300, now=1060)
        assert not s.restarts(), s.restarts()


def test_a_frozen_tx_counter_restarts_the_getty():
    with tempfile.TemporaryDirectory() as tmp:
        s = Station(tmp)
        s.tick(64925, now=1000)
        assert not s.restarts(), "one sample proves nothing"
        s.tick(64925, now=1030)
        assert any("serial-getty@ttyS1" in c for c in s.restarts()), s.restarts()


def test_it_does_not_restart_again_within_the_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        s = Station(tmp)
        s.tick(64925, now=1000)
        s.tick(64925, now=1030)
        n = len(s.restarts())
        s.tick(64925, now=1060)
        s.tick(64925, now=1090)
        assert len(s.restarts()) == n, "cooldown ignored: %s" % s.restarts()


def test_it_tries_again_after_the_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        s = Station(tmp)
        s.tick(64925, now=1000)
        s.tick(64925, now=1030)
        s.tick(64925, now=1400)
        assert len(s.restarts()) == 2, s.restarts()


def test_no_getty_no_restart():
    """Nothing to restart if the prompt is not running (undetected port)."""
    with tempfile.TemporaryDirectory() as tmp:
        s = Station(tmp)
        s.write_active("inactive")
        s.tick(64925, now=1000)
        s.tick(64925, now=1030)
        assert not s.restarts(), s.restarts()


def test_no_serial_console_means_nothing_to_watch():
    with tempfile.TemporaryDirectory() as tmp:
        s = Station(tmp)
        with open(s.console, "w") as f:
            f.write("tty0\n")
        s.tick(64925, now=1000)
        s.tick(64925, now=1030)
        assert not s.restarts(), s.restarts()


def _unit(name):
    return open(os.path.join(_PAYLOAD, name)).read()


def test_the_watchdog_is_installed_and_enabled():
    timer = _unit("mezz-flash-watchdog.timer")
    svc = _unit("mezz-flash-watchdog.service")
    deploy = _unit("deploy.sh")
    assert "OnUnitActiveSec=30" in timer, timer
    assert "serial_watchdog.sh" in svc, svc
    assert "enable --now mezz-flash-watchdog.timer" in deploy, deploy


def test_a_logged_in_console_is_never_restarted():
    """Restarting the getty kills the session. An operator logged in on SOL is
    also the one case where the frozen counter may be their own idle shell."""
    with tempfile.TemporaryDirectory() as tmp:
        st = Station(tmp)
        st.login("ttyS1")
        st.tick(64925, now=1000)
        r = st.tick(64925, now=1030)
        assert not st.restarts(), st.restarts()
        assert "login session" in r.stdout, r.stdout


def test_a_session_on_another_tty_does_not_protect_this_one():
    with tempfile.TemporaryDirectory() as tmp:
        st = Station(tmp)
        st.login("tty1")
        st.tick(64925, now=1000)
        st.tick(64925, now=1030)
        assert any("serial-getty@ttyS1" in c for c in st.restarts()), st.restarts()


def test_it_restarts_once_the_operator_logs_out():
    with tempfile.TemporaryDirectory() as tmp:
        st = Station(tmp)
        st.login("ttyS1")
        st.tick(64925, now=1000)
        st.tick(64925, now=1030)
        assert not st.restarts(), st.restarts()
        open(st.who, "w").close()          # logged out
        st.tick(64925, now=1060)
        assert any("serial-getty@ttyS1" in c for c in st.restarts()), st.restarts()


def test_an_explicit_tty_argument_overrides_the_console():
    """post runs the station watchdog from a transient timer and passes the
    tty it found, rather than relying on console= ordering. The argument
    must win over /sys/class/tty/console/active."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.mkdir(bindir)
        calls = os.path.join(tmp, "calls.log")
        for name, body in (("systemctl",
                            'echo "systemctl $*" >> "%s"\n'
                            '[ "$1" = "is-active" ] && echo active\nexit 0\n' % calls),
                           ("who", "exit 0\n"), ("loginctl", "exit 0\n")):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write("#!/bin/bash\n" + body)
            os.chmod(p, 0o755)
        active = os.path.join(tmp, "active")
        with open(active, "w") as f:
            f.write("tty0 ttyS1\n")          # console says ttyS1 ...
        info = os.path.join(tmp, "serial")
        with open(info, "w") as f:           # ... ttyS0 is the one frozen
            f.write("serinfo:1.0\n0: uart:16550A port:000003F8 irq:4 tx:500 rx:0\n"
                    "1: uart:16550A port:000002F8 irq:3 tx:900 rx:0\n")
        env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"],
                   MEZZ_SERIAL_INFO=info, MEZZ_CONSOLE_ACTIVE=active,
                   MEZZ_WD_STATE=os.path.join(tmp, "state"), MEZZ_WD_COOLDOWN="0")
        for now in ("1000", "1030"):
            subprocess.run(["bash", _WD, "ttyS0"], env=dict(env, MEZZ_WD_NOW=now),
                           capture_output=True, text=True)
        restarts = [l for l in open(calls).read().splitlines() if "restart" in l] \
            if os.path.exists(calls) else []
        assert restarts == ["systemctl restart serial-getty@ttyS0.service"], restarts
