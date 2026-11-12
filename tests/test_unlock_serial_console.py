"""The SOL console must exist on every boot, and the station must not hold it.

et9b1, 2026-09-22: on 9 of 16 recorded boots the kernel left the SOL UART
(ttyS1 at 0x2f8, the BMC's virtual UART) as `uart:unknown`, so systemd never
started a serial getty and SOL showed nothing. A late re-probe
(`setserial /dev/ttyS1 port 0x2f8 irq 3 autoconfig auto_irq`) brought it back
clean, IRQ firing; forcing the type without autoconfig left TX dead (tx:0),
and polled mode (irq 0) dropped about half the characters.

Operator decisions the same day: no second serial / debug cable exists for
operators, so the fix is software on the SOL port itself; and the station must
not hold boot, so the prompt and live progress are on SOL while it works.
"""
import os
import subprocess
import tempfile

_PAYLOAD = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "unlock-with-ubuntu", "payload")
_FIXUP = os.path.join(_PAYLOAD, "serial_console_fixup.sh")

_UNKNOWN = """serinfo:1.0 driver revision:
0: uart:16550A port:000003F8 irq:4 tx:0 rx:0 CTS|DSR|CD
1: uart:unknown port:000002F8 irq:3
2: uart:unknown port:000003E8 irq:4
"""
_GOOD = _UNKNOWN.replace("1: uart:unknown port:000002F8 irq:3",
                         "1: uart:16550A port:000002F8 irq:3 tx:9 rx:0 CTS|DSR|CD")


def _fixup(serial_info, console_active="tty0 ttyS1"):
    with tempfile.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.mkdir(bindir)
        log = os.path.join(tmp, "calls.log")
        for name in ("setserial", "systemctl"):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write('#!/bin/bash\necho "%s $*" >> "%s"\n' % (name, log))
            os.chmod(p, 0o755)
        info = os.path.join(tmp, "serial")
        with open(info, "w") as f:
            f.write(serial_info)
        active = os.path.join(tmp, "active")
        with open(active, "w") as f:
            f.write(console_active + "\n")
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   MEZZ_SERIAL_INFO=info, MEZZ_CONSOLE_ACTIVE=active)
        got = subprocess.run(["bash", _FIXUP], capture_output=True, text=True,
                             env=env)
        calls = open(log).read().splitlines() if os.path.exists(log) else []
        return got, calls


def test_an_undetected_sol_uart_is_reprobed_with_its_irq():
    got, calls = _fixup(_UNKNOWN)
    assert "setserial /dev/ttyS1 port 0x2f8 irq 3 autoconfig auto_irq" in calls, (
        calls, got.stdout, got.stderr)


def test_the_serial_getty_is_started_after_the_reprobe():
    _, calls = _fixup(_UNKNOWN)
    starts = [c for c in calls if c.startswith("systemctl") and
              "serial-getty@ttyS1.service" in c]
    assert starts, calls
    assert calls.index(starts[0]) > [i for i, c in enumerate(calls)
                                     if c.startswith("setserial")][0], calls


def test_a_detected_uart_is_left_alone():
    _, calls = _fixup(_GOOD)
    assert not [c for c in calls if c.startswith("setserial")], calls


def test_the_port_follows_the_kernel_console_not_a_hardcoded_tty():
    """Tioga Pass and Leopard differ; the SOL port is whatever console= says."""
    info = _UNKNOWN.replace("0: uart:16550A port:000003F8 irq:4 tx:0 rx:0 CTS|DSR|CD",
                            "0: uart:unknown port:000003F8 irq:4")
    _, calls = _fixup(info, console_active="tty0 ttyS0")
    assert "setserial /dev/ttyS0 port 0x3f8 irq 4 autoconfig auto_irq" in calls, calls


def test_no_serial_console_means_nothing_to_do():
    _, calls = _fixup(_UNKNOWN, console_active="tty0")
    assert not calls, calls


def _unit(name):
    return open(os.path.join(_PAYLOAD, name)).read()


def test_the_station_does_not_hold_boot():
    u = _unit("mezz-flash.service")
    assert "Type=oneshot" not in u, u
    assert "Type=simple" in u or "Type=exec" in u, u
    assert "getty.target" in u, "must start after the login prompts exist"


def test_the_fixup_runs_after_the_station_so_it_cannot_hang_boot():
    """et9b1 16:20, 2026-09-22: re-probing the SOL UART EARLY in boot woke it
    in a TX-stalled state, and with console=ttyS1 every PID1 status line then
    blocked ~30s -- ssh came up 22 minutes in. Operator decision the same day:
    keep the fixup, but run it after mezz-flash.service has started, i.e. once
    the boot is already through multi-user, and never write to the console."""
    u = _unit("mezz-flash-serial.service")
    assert "After=" in u and "mezz-flash.service" in u.split("[Service]")[0], u
    assert "Before=getty.target" not in u, "must not run before the prompts now"
    assert "WantedBy=multi-user.target" in u, u
    assert not [l for l in u.splitlines() if l.startswith("Standard")
                and "console" in l], u
    deploy = _unit("deploy.sh")
    assert "enable" in deploy and "mezz-flash-serial" in deploy, deploy
    assert "disable mezz-flash-serial.service" not in deploy, deploy


def test_debians_setserial_services_are_masked():
    """Its shutdown save + boot restore is half of the race above."""
    deploy = _unit("deploy.sh")
    assert "mask setserial.service etc-setserial.service" in deploy, deploy
    assert "autoserial.conf" in deploy, deploy


def _fixup_verify(serial_infos, console_active="tty0 ttyS1"):
    """Run the fixup with a /proc file whose contents advance per read, so the
    script's own before/after tx sampling can be simulated."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as tmp:
        bindir = os.path.join(tmp, "bin")
        os.mkdir(bindir)
        log = os.path.join(tmp, "calls.log")
        seq = os.path.join(tmp, "seq")
        with open(seq, "w") as f:
            f.write("0\n")
        info = os.path.join(tmp, "serial")
        with open(info, "w") as f:
            f.write(serial_infos[0])
        # each `grep` of the info file advances it to the next canned state
        p_ = os.path.join(bindir, "nextstate")
        with open(p_, "w") as f:
            f.write("#!/bin/bash\nn=$(cat %s); n=$((n+1)); echo $n > %s\n"
                    "case $n in\n" % (seq, seq) +
                    "".join("%d) cat %s/state%d ;;\n" % (i + 1, tmp, i + 1)
                            for i in range(len(serial_infos) - 1)) +
                    "*) cat %s/state%d ;;\nesac\n" % (tmp, len(serial_infos) - 1))
        os.chmod(p_, 0o755)
        for i, body in enumerate(serial_infos[1:], start=1):
            with open(os.path.join(tmp, "state%d" % i), "w") as f:
                f.write(body)
        for name in ("setserial", "systemctl", "agetty"):
            q = os.path.join(bindir, name)
            with open(q, "w") as f:
                f.write('#!/bin/bash\necho "%s $*" >> "%s"\n'
                        '[ "$1" = "is-active" ] && { echo active; exit 0; }\n'
                        'exit 0\n' % (name, log))
            os.chmod(q, 0o755)
        active = os.path.join(tmp, "active")
        with open(active, "w") as f:
            f.write(console_active + "\n")
        env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
                   MEZZ_SERIAL_INFO=info, MEZZ_CONSOLE_ACTIVE=active,
                   MEZZ_TX_PROBE_INFO=p_)
        got = subprocess.run(["bash", _FIXUP], capture_output=True, text=True,
                             env=env)
        calls = open(log).read().splitlines() if os.path.exists(log) else []
        return got, calls


_TX0 = _UNKNOWN.replace("1: uart:unknown port:000002F8 irq:3",
                        "1: uart:16550A port:000002F8 irq:3 tx:0 rx:0 CTS|DSR|CD")
_TX9 = _UNKNOWN.replace("1: uart:unknown port:000002F8 irq:3",
                        "1: uart:16550A port:000002F8 irq:3 tx:900 rx:0 CTS|DSR|CD")


def test_a_reprobed_port_that_transmits_is_kept():
    got, calls = _fixup_verify([_UNKNOWN, _TX0, _TX9])
    assert any("systemctl" in c and "start" in c for c in calls), calls
    assert not [c for c in calls if "uart none" in c], calls


def test_a_reprobed_port_that_still_does_not_transmit_is_undone():
    """A dead-but-fast console beats an alive-but-blocking one: with
    console=ttyS1, writes to a stalled port cost PID1 ~30s each."""
    got, calls = _fixup_verify([_UNKNOWN, _TX0, _TX0])
    assert [c for c in calls if "uart none" in c], calls
    assert [c for c in calls if c.startswith("systemctl stop")], calls
    assert "not transmitting" in got.stdout, got.stdout


def test_the_verify_step_generates_its_own_traffic():
    """et9b1 21:19, 2026-09-22: the check relied on `agetty --reload` for the
    traffic it measures, but the getty is started with --no-block and may not
    be up yet -- a healthy port then reads as dead. Write to the tty directly,
    under a timeout so a stalled port cannot block the unit."""
    body = open(_FIXUP).read()
    assert "MEZZ_TTY_DEV" in body, "tty device must be overridable for tests"
    i = body.index("before=$(txnow)")
    j = body.index("after=$(txnow)")
    between = body[i:j]
    assert "timeout" in between and ">" in between, between


def test_the_two_units_do_not_form_an_ordering_cycle():
    """et9b4, 22:02 2026-09-22: mezz-flash.service still had
    After=mezz-flash-serial.service from the old design while the fixup had
    gained After=mezz-flash.service. systemd broke the loop by deleting the
    station's job -- "Job mezz-flash.service/start deleted to break ordering
    cycle" -- so the station never ran and a locked card never went dark."""
    station = _unit("mezz-flash.service")
    fixup = _unit("mezz-flash-serial.service")
    station_head = station.split("[Service]")[0]
    fixup_head = fixup.split("[Service]")[0]
    assert "mezz-flash.service" in fixup_head, "the fixup runs after the station"
    for line in station_head.splitlines():
        if line.startswith("After=") or line.startswith("Before="):
            assert "mezz-flash-serial" not in line, ("cycle: %s" % line)
