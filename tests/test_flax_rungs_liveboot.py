import os
import subprocess
from types import SimpleNamespace

from flax_rack.creds import Creds
from flax_rungs import hostssh, liveboot, redfish

PW = "s3cretpw"


OLD, NEW = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
MAC = "aa:bb:cc:dd:ee:01"
LIVE = "initrd=x BOOTIF=01-aa-bb-cc-dd-ee-01 pxeaddr=1.2.3.4 quiet"
UBUNTU = "BOOT_IMAGE=/vmlinuz root=/dev/sda2 quiet"


class Fake:
    """Fake runner on a fake clock: records argv/env, reads any passed FD, answers by
    tool. The host model: the OLD OS (boot id OLD, uptime days, cmdline `old_cmdline`)
    keeps answering ssh until `down_after` s past the power action, is dark until
    `up_after` s, then a FRESH boot (boot id NEW, booted `boot_after` s after the
    action, cmdline `cmdline`). down_after=None: the action never took (old OS forever)."""
    def __init__(self, power="on", http="200", cmdline=LIVE, ssh_ok=True,
                 old_cmdline=UBUNTU, down_after=4, up_after=200, boot_after=60,
                 old_uptime=86400.0, new_boot_id=NEW, pre_ssh=True):
        self.calls, self.netrc, self.power, self.http = [], [], power, http
        self.cmdline, self.ssh_ok, self.old_cmdline = cmdline, ssh_ok, old_cmdline
        self.down_after, self.up_after, self.boot_after = down_after, up_after, boot_after
        self.old_uptime, self.new_boot_id, self.pre_ssh = old_uptime, new_boot_id, pre_ssh
        self.t, self.acted_at, self.probes = [0.0], None, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        for fd in kw.get("pass_fds", ()):
            self.netrc.append(os.read(fd, 4096).decode())
        t, now = argv[0], self.t[0]
        if t == "curl":
            return SimpleNamespace(returncode=0, stdout=self.http, stderr="")
        if t == "ipmitool":
            if self.power is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="no session")
            if "status" in argv:
                return SimpleNamespace(returncode=0, stderr="",
                                       stdout=f"Chassis Power is {self.power}")
            if argv[-1] in ("cycle", "on"):
                self.acted_at = now
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if t == "sshpass":
            banner = "Warning: Permanently added '10.0.0.2' (ED25519) to the list of known hosts.\n"
            if not self.ssh_ok:
                return SimpleNamespace(returncode=255, stdout="", stderr="refused")
            if self.acted_at is None:
                if not self.pre_ssh or self.power != "on":
                    return SimpleNamespace(returncode=255, stdout="", stderr="refused")
                os_ = "old"
            else:
                dt = now - self.acted_at
                if self.down_after is None or dt < self.down_after:
                    os_ = "old"
                elif dt < self.up_after:
                    return SimpleNamespace(returncode=255, stdout="", stderr="No route to host")
                else:
                    os_ = "new"
            self.probes.append((now, os_))
            if os_ == "old":
                out = f"{OLD}\n{self.old_uptime + now:.2f}\n{self.old_cmdline}\n"
            else:
                up = now - self.acted_at - self.boot_after
                out = f"{self.new_boot_id}\n{up:.2f}\n{self.cmdline}\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr=banner)
        return SimpleNamespace(returncode=0, stdout="", stderr="")  # ping


def creds():
    return Creds(("s", "s"), [("bu", PW)], ("flax", PW), [("rfu", PW)])


def run_boot(fake, expect_mac=MAC, **kw):
    def sleep(s): fake.t[0] += s
    return liveboot.boot_live("10.0.0.1", "10.0.0.2", creds(), expect_mac=expect_mac,
                              runner=fake, sleep=sleep, clock=lambda: fake.t[0], **kw)


def no_pw_in_argv(fake):
    for argv, _ in fake.calls:
        assert not any(PW in a for a in argv), argv


def test_password_never_in_argv_netrc_on_fd():
    f = Fake()
    r = run_boot(f)
    assert r.verdict == "pass", r.detail
    no_pw_in_argv(f)
    assert f.netrc and PW in f.netrc[0] and "rfu" in f.netrc[0]
    curl = next(a for a, _ in f.calls if a[0] == "curl")
    assert "--netrc-file" in curl and curl[curl.index("--netrc-file") + 1].startswith("/dev/fd/")
    assert '"Pxe"' in curl[curl.index("-d") + 1]


def test_power_cycle_if_on_else_on():
    f = Fake(power="on"); run_boot(f)
    assert ["chassis", "power", "cycle"] == next(a for a, _ in f.calls if "cycle" in a)[-3:]
    f = Fake(power="off"); r = run_boot(f)
    assert r.verdict == "pass", r.detail
    assert r.detail["power_action"] == "on" and r.detail["old_boot_id"] is None


def test_power_state_unknown_refuses_to_act():
    f = Fake(power=None)
    r = run_boot(f)
    assert r.verdict == "unmeasured" and "power state unknown" in r.detail["headline"]
    assert not [a for a, _ in f.calls if a[0] == "curl" or a[-1] in ("on", "cycle")]


def test_old_live_image_answering_first_is_never_judged():
    # re-run while already in the live image: the dying OS has the right cmdline + MAC.
    f = Fake(old_cmdline=LIVE)
    r = run_boot(f)
    assert r.verdict == "pass", r.detail
    assert f.probes[0] == (0.0, "old") and f.probes[1][1] == "old"   # pre + first poll
    assert r.detail["boot_id"] == NEW and r.detail["old_boot_id"] == OLD
    assert r.detail["ssh_after_s"] >= 200 and r.detail["uptime_s"] < r.detail["ssh_after_s"]


def test_installed_os_answering_first_keeps_polling():
    # RRORD-101 Ubuntu answers ssh right after `power cycle`: not a "not the live image" fail
    f = Fake(old_cmdline=UBUNTU)
    r = run_boot(f)
    assert r.verdict == "pass", r.detail
    assert any(o == "old" for _, o in f.probes[1:])


def test_power_action_that_never_took_fails_at_deadline():
    f = Fake(old_cmdline=LIVE, down_after=None)
    r = run_boot(f, deadline_s=300)
    assert r.verdict == "fail" and "only the pre-reboot OS answered" in r.detail["error"]
    assert "boot_id unchanged" in r.detail["last"]["stale"]


def test_uptime_alone_catches_old_os_when_boot_id_unknown():
    # ssh was down before the action (no old boot id); old OS still answers after it
    f = Fake(old_cmdline=LIVE, down_after=None, pre_ssh=False)
    r = run_boot(f, deadline_s=300)
    assert r.verdict == "fail" and r.detail["old_boot_id"] is None
    assert "uptime" in r.detail["last"]["stale"]


def test_unchanged_boot_id_alone_is_stale():
    # an implausible short uptime must not let the same boot through
    f = Fake(old_cmdline=LIVE, down_after=None, old_uptime=-1000.0)
    r = run_boot(f, deadline_s=120)
    assert r.verdict == "fail" and r.detail["last"]["stale"] == "boot_id unchanged"


def test_fresh_boot_of_installed_os_is_not_live():
    r = run_boot(Fake(cmdline=UBUNTU))
    assert r.verdict == "fail" and "not the live image" in r.detail["error"]
    assert r.detail["cmdline_ok"] is False


def test_wrong_host_at_ip_fails_on_bootif():
    r = run_boot(Fake(cmdline=LIVE.replace("ee-01", "ee-99")))
    assert r.verdict == "fail"
    assert r.detail["error"] == ("wrong host at 10.0.0.2: BOOTIF aa:bb:cc:dd:ee:99 "
                                 "!= discovered aa:bb:cc:dd:ee:01")


def test_bootif_mac_formats():
    assert liveboot.bootif_mac("a BOOTIF=01-AA-BB-CC-DD-EE-01 b") == "aabbccddee01"
    assert liveboot.bootif_mac("BOOTIF=01-aa:bb:cc:dd:ee:01") == "aabbccddee01"
    assert liveboot.bootif_mac("BOOTIF=aa-bb") is None and liveboot.bootif_mac("") is None


def test_no_discovered_mac_refuses():
    f = Fake()
    r = run_boot(f, expect_mac=None)
    assert r.verdict == "fail" and "discover first" in r.detail["error"]
    assert not [a for a, _ in f.calls if a[0] in ("curl", "ipmitool")]


def test_redfish_fail_falls_back_to_ipmitool_bootdev():
    f = Fake(http="401")
    r = redfish.set_boot_pxe("10.0.0.1", [("u", PW)], runner=f)
    assert r.verdict == "pass" and r.detail["method"] == "ipmitool"
    assert any("bootdev" in a for a, _ in f.calls)


def test_disable_body():
    f = Fake()
    redfish.set_boot_pxe("10.0.0.1", [("u", PW)], enable=False, runner=f)
    body = next(a for a, _ in f.calls if a[0] == "curl")
    assert "Disabled" in body[body.index("-d") + 1]


def test_deadline_fails_with_last():
    f = Fake(ssh_ok=False)
    r = run_boot(f, deadline_s=60)
    assert r.verdict == "fail" and "no ssh to a fresh boot" in r.detail["error"]
    assert set(r.detail["last"]) == {"power", "ping", "ssh", "stale"}
    assert r.detail["last"]["power"] == "on" and r.detail["last"]["ssh"].startswith("rc=255")
    no_pw_in_argv(f)


def test_run_timeout_is_rc124():
    def boom(argv, **kw): raise subprocess.TimeoutExpired(argv, 1)
    assert hostssh.run("h", "u", PW, "true", runner=boom)[:2] == (124, "")


def test_hostssh_splits_stdout_stderr_and_quiets_ssh():
    seen = []
    def fake(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "out\n", "Warning: Permanently added 'h'\n")
    rc, out, err = hostssh.run("h", "u", PW, "true", runner=fake)
    assert (rc, out) == (0, "out\n") and "Permanently added" in err
    assert "LogLevel=ERROR" in seen[0]
