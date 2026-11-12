# tests/test_flax_reconcile_drivers.py
"""Tests for the write-capable switch driver classes.

Runners are injectable so no real switch is needed. time.sleep is patched
in flap tests so the test suite doesn't actually pause.
"""
from flax_reconcile import drivers as _drivers
from flax_reconcile.drivers import (
    AristaEAPI, IOS, Cumulus, _make_cumulus_runner, _make_ios_runner,
)


def test_ios_runner_argv_has_no_controlmaster(monkeypatch):
    """The IOS write runner must NOT use ssh ControlMaster/ControlPersist: under
    the container's PID-1 python a backgrounded master ssh reparents and leaks
    as a zombie (see the switch-sense leak). Each call is a plain foreground ssh
    that subprocess fully reaps; the runner already sends the whole config
    session in one payload, so no warm socket is needed."""
    captured = {}

    def fake_check_output(argv, **kw):
        captured["argv"] = argv
        return ""

    monkeypatch.setattr(_drivers.subprocess, "check_output", fake_check_output)
    runner = _make_ios_runner("turtle-gouda", "admin", "pw")
    runner("interface GigabitEthernet1/0/6\nshutdown\n")
    joined = " ".join(captured["argv"])
    assert "ControlMaster" not in joined
    assert "ControlPersist" not in joined
    assert "ControlPath" not in joined
    assert captured["argv"][0] == "sshpass"
    assert "admin@turtle-gouda" in captured["argv"]
    # the classic-IOS kex/hostkey workarounds must stay
    assert "KexAlgorithms=+diffie-hellman-group14-sha1" in joined
    assert "HostKeyAlgorithms=+ssh-rsa" in joined


def test_arista_flap_issues_shut_then_noshut(monkeypatch):
    calls = []
    d = AristaEAPI("rabbit-fiesta", caller=lambda batch: calls.append(batch))
    slept = []
    monkeypatch.setattr("flax_reconcile.drivers.time.sleep", lambda s: slept.append(s))
    d.flap("Ethernet6/1", hold_seconds=2)
    assert any("shutdown" in c and "no shutdown" not in c for c in map(str, calls))
    assert any("no shutdown" in str(c) for c in calls)
    assert slept == [2]


def test_ios_flap_uses_to_iface_and_holds(monkeypatch):
    cmds = []
    d = IOS("turtle-blues", runner=lambda s: cmds.append(s),
            to_iface=lambda p: "GigabitEthernet1/0/6")
    monkeypatch.setattr("flax_reconcile.drivers.time.sleep", lambda s: None)
    d.flap("6", hold_seconds=2)
    assert "shutdown" in cmds[0] and "GigabitEthernet1/0/6" in cmds[0]
    assert "no shutdown" in cmds[1]


def test_cumulus_flap_down_sleep_up():
    cmds = []
    d = Cumulus("turtle-rhythm", runner=lambda s: cmds.append(s))
    d.flap("swp6", hold_seconds=1)
    assert "ip link set swp6 down" in cmds[0]
    assert "ip link set swp6 up" in cmds[0]


def test_arista_set_access_vlan_writes_memory():
    calls = []
    d = AristaEAPI("rabbit-fiesta", caller=lambda batch: calls.append(batch))
    d.set_access_vlan("Ethernet6/1", 17)
    flat = " ".join(map(str, calls))
    assert "switchport access vlan 17" in flat and "write memory" in flat


def test_arista_set_access_vlan_full_sequence():
    """Verify the exact eAPI batch: configure terminal, interface, vlan, end, write memory."""
    calls = []
    d = AristaEAPI("rabbit-fiesta", caller=lambda batch: calls.append(batch))
    d.set_access_vlan("Ethernet6/1", 17)
    assert len(calls) == 1
    batch = calls[0]
    assert batch == [
        "configure terminal",
        "interface Ethernet6/1",
        "switchport access vlan 17",
        "end",
        "write memory",
    ]


def test_arista_flap_two_batches_no_shutdown_in_second(monkeypatch):
    """Shutdown and no-shutdown are in separate eAPI batches, not the same call."""
    calls = []
    d = AristaEAPI("rabbit-fiesta", caller=lambda batch: calls.append(batch))
    monkeypatch.setattr("flax_reconcile.drivers.time.sleep", lambda s: None)
    d.flap("Ethernet6/1", hold_seconds=2)
    assert len(calls) == 2
    # First batch: shutdown
    assert "shutdown" in calls[0]
    assert "no shutdown" not in calls[0]
    # Second batch: no shutdown
    assert "no shutdown" in calls[1]


def test_ios_set_access_vlan_writes_memory():
    cmds = []
    d = IOS("turtle-blues", runner=lambda s: cmds.append(s),
            to_iface=lambda p: "GigabitEthernet1/0/6")
    d.set_access_vlan("swp6", 17)
    assert len(cmds) == 1
    cmd = cmds[0]
    assert "switchport access vlan 17" in cmd
    assert "write memory" in cmd
    assert "GigabitEthernet1/0/6" in cmd


def test_ios_flap_two_runner_calls(monkeypatch):
    """shutdown and no shutdown are two separate runner invocations."""
    cmds = []
    d = IOS("turtle-blues", runner=lambda s: cmds.append(s),
            to_iface=lambda p: "GigabitEthernet1/0/6")
    monkeypatch.setattr("flax_reconcile.drivers.time.sleep", lambda s: None)
    d.flap("swp6", hold_seconds=2)
    assert len(cmds) == 2
    assert "shutdown" in cmds[0] and "no shutdown" not in cmds[0]
    assert "no shutdown" in cmds[1]


def test_cumulus_set_access_vlan_two_commands():
    """net add + net commit are two separate runner calls."""
    cmds = []
    d = Cumulus("turtle-rhythm", runner=lambda s: cmds.append(s))
    d.set_access_vlan("swp6", 17)
    assert len(cmds) == 2
    assert "net add interface swp6 bridge access 17" in cmds[0]
    assert "net commit" in cmds[1]


def test_flap_hold_seconds_default_is_2(monkeypatch):
    """FLAP_HOLD_SECONDS default = 2, matching reaper_leased."""
    slept = []
    monkeypatch.setattr("flax_reconcile.drivers.time.sleep", lambda s: slept.append(s))
    d = AristaEAPI("rabbit-fiesta", caller=lambda batch: None)
    d.flap("Ethernet6/1")  # no hold_seconds arg
    assert slept == [2]


def test_cumulus_set_access_vlan_exact_commands():
    """set_access_vlan issues net add ... bridge access <vid> then net commit
    as two separate runner calls (NCLU workflow)."""
    cmds = []
    d = Cumulus("turtle-rhythm", runner=lambda s: cmds.append(s))
    d.set_access_vlan("swp6", 42)
    assert len(cmds) == 2
    assert cmds[0] == "net add interface swp6 bridge access 42"
    assert cmds[1] == "net commit"


def test_cumulus_flap_uses_sudo_ip_link():
    """flap uses 'sudo ip link set <port> down/up' (needs root on Cumulus)
    in a single shell command with a switch-side sleep between them."""
    cmds = []
    d = Cumulus("turtle-rhythm", runner=lambda s: cmds.append(s))
    d.flap("swp6", hold_seconds=3)
    assert len(cmds) == 1
    cmd = cmds[0]
    assert "sudo ip link set swp6 down" in cmd
    assert "sudo ip link set swp6 up" in cmd
    assert "sleep 3" in cmd


def test_cumulus_flap_hold_seconds_default_is_1():
    """Cumulus flap defaults hold_seconds=1 (single-command switch-side sleep)."""
    cmds = []
    d = Cumulus("turtle-rhythm", runner=lambda s: cmds.append(s))
    d.flap("swp6")  # no hold_seconds arg
    assert "sleep 1" in cmds[0]


def test_arista_set_admin_up_issues_no_shutdown():
    """set_admin_up issues a single eAPI batch with `no shutdown` (no sleep,
    no second shutdown batch) -- the un-strand path."""
    calls = []
    d = AristaEAPI("rabbit-lorax", caller=lambda batch: calls.append(batch))
    d.set_admin_up("Ethernet8/3")
    assert calls == [[
        "configure terminal",
        "interface Ethernet8/3",
        "no shutdown",
        "end",
    ]]


def test_ios_set_admin_up_uses_to_iface_no_shutdown():
    """IOS set_admin_up maps the port via _to_iface and issues `no shutdown`."""
    cmds = []
    d = IOS("turtle-blues", runner=lambda s: cmds.append(s),
            to_iface=lambda p: "GigabitEthernet1/0/8")
    d.set_admin_up("swp8")
    assert len(cmds) == 1
    assert "no shutdown" in cmds[0]
    assert "GigabitEthernet1/0/8" in cmds[0]
    assert "shutdown\nend" not in cmds[0].replace("no shutdown", "")


def test_cumulus_set_admin_up_ip_link_up():
    """Cumulus set_admin_up issues `sudo ip link set <port> up` only (no down)."""
    cmds = []
    d = Cumulus("turtle-rhythm", runner=lambda s: cmds.append(s))
    d.set_admin_up("swp8")
    assert cmds == ["sudo ip link set swp8 up"]


def test_make_cumulus_runner_returns_callable_without_ssh():
    """_make_cumulus_runner builds a closure; construction does NOT invoke ssh."""
    runner = _make_cumulus_runner("turtle-x", "cumulus", "CumulusLinux!")
    assert callable(runner)


def test_make_cumulus_runner_redacts_password(monkeypatch):
    """On ssh failure the error message must NOT contain the password."""
    import subprocess
    import flax_reconcile.drivers as d

    def boom(*args, **kwargs):
        # Simulate a CalledProcessError from sshpass+ssh
        raise subprocess.CalledProcessError(255, args[0])

    monkeypatch.setattr(subprocess, "check_output", boom)
    runner = _make_cumulus_runner("turtle-x", "cumulus", "SuperSecret!")
    from flax_reconcile.drivers import SwitchUnreachable
    try:
        runner("net show hostname")
        assert False, "should have raised SwitchUnreachable"
    except SwitchUnreachable as exc:
        assert "SuperSecret!" not in str(exc)
        assert "SuperSecret!" not in repr(exc)


def _capture(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, **kw)
        return ""
    monkeypatch.setattr(_drivers.subprocess, "check_output", fake)
    return seen


def test_ios_runner_password_in_env_not_argv(monkeypatch):
    seen = _capture(monkeypatch)
    _make_ios_runner("10.0.0.1", "admin", "fake-ios-pw")("show clock")
    assert seen["argv"][:2] == ["sshpass", "-e"]
    assert not any("fake-ios-pw" in a for a in seen["argv"])
    assert seen["env"]["SSHPASS"] == "fake-ios-pw"


def test_cumulus_runner_password_in_env_not_argv(monkeypatch):
    seen = _capture(monkeypatch)
    _make_cumulus_runner("10.0.0.1", "cumulus", "fake-cum-pw")("net show hostname")
    assert seen["argv"][:2] == ["sshpass", "-e"]
    assert not any("fake-cum-pw" in a for a in seen["argv"])
    assert seen["env"]["SSHPASS"] == "fake-cum-pw"
