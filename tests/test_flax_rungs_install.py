"""install + firstboot rungs on fakes: a fake clock, a fake BMC/host runner, and
nginx/dnsmasq logs the fake writes as fake time passes (lines appear only once their
time has come, like the real logs)."""
import json
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from flax_rack.creds import Creds
from flax_rungs import install, pxeboot

PW = "p"
FIX = Path(__file__).parent / "fixtures" / "rackpost" / "pxe"
BASE = float(int(time.time()) - 86400)   # fake epoch at fake clock 0 (dnsmasq lines carry no year)
HOST, BMC = "10.9.0.5", "10.9.1.5"
NAME = "rr101-c37"
MAC = "b8:59:9f:00:00:01"
OLD, NEW = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
UBUNTU = "BOOT_IMAGE=/vmlinuz-6.8.0-31-generic root=/dev/mapper/ubuntu--vg-ubuntu--lv ro"
LIVE = "initrd=x BOOTIF=01-b8-59-9f-00-00-01 pxeaddr=1.2.3.4"
BLK_NVME = ('blk=NAME="ubuntu--vg-ubuntu--lv" TYPE="lvm" TRAN="" RM="0"\n'
            'blk=NAME="nvme0n1p3" TYPE="part" TRAN="" RM="0"\n'
            'blk=NAME="nvme0n1" TYPE="disk" TRAN="nvme" RM="0"\n')


def creds(host=("flax", PW)):
    return Creds(("s", "s"), [("bu", PW)], host, [("rfu", PW)])


def _nginx(ip, ts, path, code=200, ua="iPXE/1.21.1+ (gc1834)"):
    stamp = time.strftime("%d/%b/%Y:%H:%M:%S +0000", time.gmtime(ts))
    return f'{ip} - - [{stamp}] "GET {path} HTTP/1.1" {code} 123 "-" "{ua}"\n'


def _dnsmasq(ip, ts):
    return (time.strftime("%b %d %H:%M:%S", time.localtime(ts)) +
            f" dnsmasq-tftp[1]: sent /srv/tftpboot/efi64/ipxe.efi to {ip}\n")


class Fake:
    """BMC + host. install model: after the power action the node blips off for
    `blip` s (cycle only), PXE boots (tftp/boot.ipxe/hostname file at +20 s), fetches
    the installer at `fetch_at`, and powers off at `off_at` (None: never). firstboot
    model: after the action the host is dark until `up_after`, then answers ssh with
    `probe` (a fresh boot `boot_after` s after the action)."""

    def __init__(self, tmp, power="on", http="204", off_at=1500.0, fetch_at=90.0,
                 fetch_code=200, blip=5.0, action_takes=True, ssh_rc=0, up_after=120.0,
                 boot_after=30.0, probe=None, old_probe=None, pxe_dir=None, efi=None,
                 partuuids=(), sudo_ok=True, readback_ok=True, after=None,
                 installed_sudo_ok=True):
        self.tmp, self.power0, self.http = tmp, power, http
        self.off_at, self.fetch_at, self.fetch_code, self.blip = off_at, fetch_at, fetch_code, blip
        self.action_takes, self.ssh_rc, self.up_after = action_takes, ssh_rc, up_after
        self.boot_after, self.probe, self.old_probe = boot_after, probe, old_probe
        self.pxe_dir = pxe_dir
        self.t = [0.0]
        self.calls, self.acted, self.action, self.pending = [], None, None, []
        self.nginx, self.dnsmasq = tmp / "access.log", tmp / "dnsmasq.log"
        self.nginx.write_text(_nginx(HOST, BASE - 600, "/boot.ipxe"))   # an older boot
        self.dnsmasq.write_text("")
        self.file_at_action = None
        # firstboot BootOrder recovery: the live image's efibootmgr, the host's PARTUUIDs,
        # an OS reboot after which the host answers `after` (spec 2026-11-11 §2)
        self.efi, self.partuuids, self.sudo_ok, self.readback_ok = efi, partuuids, sudo_ok, readback_ok
        self.after, self.installed_sudo_ok = after, installed_sudo_ok
        self.rebooted, self.writes, self.scripts = None, [], []

    def clock(self):
        return self.t[0]

    def wall(self):
        return BASE + self.t[0]

    def sleep(self, s):
        self.t[0] += s
        self._flush()

    def _flush(self):
        now = BASE + self.t[0]
        due = [x for x in self.pending if x[0] <= now]
        self.pending = [x for x in self.pending if x[0] > now]
        for ts, kind, line in sorted(due):
            with open(self.nginx if kind == "n" else self.dnsmasq, "a") as f:
                f.write(line)

    def _power(self):
        if self.acted is None:
            return self.power0
        dt = self.t[0] - self.acted
        if not self.action_takes:
            return "off" if self.action == "on" else self.power0
        if self.action == "cycle" and dt < self.blip:
            return "off"
        if self.off_at is not None and dt >= self.off_at:
            return "off"
        return "on"

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        self._flush()
        tool = argv[0]
        if tool == "curl":
            return SimpleNamespace(returncode=0, stdout=self.http, stderr="")
        if tool == "ipmitool":
            if self.power0 is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="no session")
            if "status" in argv:
                return SimpleNamespace(returncode=0, stderr="",
                                       stdout=f"Chassis Power is {self._power()}")
            if argv[-1] in ("cycle", "on"):
                self._act(argv[-1])
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if tool == "sshpass":
            return self._ssh(argv, kw.get("input") or "")
        return SimpleNamespace(returncode=1, stdout="", stderr="")    # ping

    def _act(self, action):
        self.acted, self.action = self.t[0], action
        if self.pxe_dir is not None:
            self.file_at_action = pxeboot.exists(NAME, pxe_dir=str(self.pxe_dir))
        if not self.action_takes:
            return
        a = BASE + self.t[0]
        self.pending += [(a + 15, "d", _dnsmasq(HOST, a + 15)),
                         (a + 20, "n", _nginx(HOST, a + 20, "/boot.ipxe")),
                         (a + 20, "n", _nginx(HOST, a + 20, f"/ipxe/hostname-{NAME}.ipxe"))]
        if self.fetch_at is not None:
            f = a + self.fetch_at
            c = self.fetch_code
            self.pending += [
                (f, "n", _nginx(HOST, f, "/install//canonical/ubuntu/24.04/ga/casper/initrd", c)),
                (f + 5, "n", _nginx(HOST, f + 5,
                                    "/install//canonical/ubuntu/24.04/ga/casper/vmlinuz", c)),
                (f + 60, "n", _nginx(HOST, f + 60, "/isos/canonical/ubuntu/24.04/"
                                     "ubuntu-24.04.4-live-server-amd64.iso", c, "Wget")),
                (f + 70, "n", _nginx(HOST, f + 70, "/install//canonical/ubuntu/24.04/"
                                     "wiwynn-tp/user-data", c, "Cloud-Init/25.2"))]

    def _ssh(self, argv, script=""):
        self.scripts.append(script)
        if "systemctl reboot" in script:
            self.rebooted = self.t[0]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "efibootmgr" in script:
            return self._efi(script)
        if self.rebooted is not None:
            dt = self.t[0] - self.rebooted
            if dt < self.up_after:
                return SimpleNamespace(returncode=255, stdout="", stderr="No route to host")
            return SimpleNamespace(returncode=0, stdout=self.after(dt - self.boot_after), stderr="")
        if self.ssh_rc:
            return SimpleNamespace(returncode=self.ssh_rc, stdout="",
                                   stderr="Permission denied, please try again.")
        if self.acted is None:
            if self.old_probe and self.power0 == "on":
                return SimpleNamespace(returncode=0, stdout=self.old_probe(self.t[0]), stderr="")
            return SimpleNamespace(returncode=255, stdout="", stderr="No route to host")
        dt = self.t[0] - self.acted
        if self.old_probe and dt < 10:
            return SimpleNamespace(returncode=0, stdout=self.old_probe(self.t[0]), stderr="")
        if dt < self.up_after:
            return SimpleNamespace(returncode=255, stdout="", stderr="No route to host")
        return SimpleNamespace(returncode=0, stdout=self.probe(dt - self.boot_after),
                               stderr="Warning: Permanently added")


    def _efi(self, script):
        installed = self.rebooted is not None or self.efi is None
        if not (self.installed_sudo_ok if installed else self.sudo_ok):
            return SimpleNamespace(returncode=1, stdout="", stderr="sudo: a password is required")
        if installed:
            return SimpleNamespace(returncode=0, stdout="BootCurrent: 0000\nBootOrder: 0000,0003\n",
                                   stderr="")
        if "efibootmgr -o" in script:
            new = script.split("efibootmgr -o", 1)[1].split()[0]
            self.writes.append(new)
            order = new if self.readback_ok else "0003,0000"
            return SimpleNamespace(returncode=0, stdout=f"BootCurrent: 0003\nBootOrder: {order}\n",
                                   stderr="")
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=self.efi + "---\n" + "".join(u + "\n" for u in self.partuuids))


def probe(boot_id=NEW, cmdline=UBUNTU, version="24.04", root="/dev/mapper/ubuntu--vg-ubuntu--lv",
          blk=BLK_NVME, macs=(MAC, "0c:42:a1:00:00:01")):
    def out(up):
        return (f"boot_id={boot_id}\nuptime={up:.2f}\ncmdline={cmdline}\n"
                f"version_id={version}\nhostname={NAME}\nroot={root}\n"
                f"macs={' '.join(macs)} \n{blk}")
    return out


@pytest.fixture
def pxe(tmp_path):
    d = tmp_path / "ipxe"
    (d / "templates").mkdir(parents=True)
    for f in ("wiwynn-tp-hmc.part", "ubuntu-24.04.ipxe"):
        shutil.copy(FIX / f, d / "templates" / f)
    return d


@pytest.fixture
def iso(tmp_path):
    root = tmp_path / "24.04"
    for rel in (*install.INSTALLER, install.AUTOINSTALL):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x")
    return root


def run_install(fake, pxe, iso, sol=None, **kw):
    kw.setdefault("deadline_s", 3600)
    return install.install(BMC, HOST, NAME, kw.pop("creds", creds()), sol_path=sol,
                           pxe_dir=str(pxe), install_root=str(iso),
                           dnsmasq=str(fake.dnsmasq), nginx=str(fake.nginx), runner=fake,
                           sleep=fake.sleep, clock=fake.clock, wall=fake.wall, **kw)


def _rows(r):
    return dict(r.detail["rows"])


def _no_pw_in_argv(fake):
    for argv, kw in fake.calls:
        assert PW not in argv, argv
        assert "-P" not in argv


# ------------------------------------------------------------------- install
def test_install_happy_path(tmp_path, pxe, iso):
    f = Fake(tmp_path, pxe_dir=pxe)
    r = run_install(f, pxe, iso)
    assert r.verdict == "pass", r.detail
    assert f.action == "cycle" and f.file_at_action is True
    assert not pxeboot.exists(NAME, pxe_dir=str(pxe))           # removed afterwards
    assert 1500 <= r.detail["off_after_s"] < 1500 + 30
    rows = _rows(r)
    assert rows["tftp (iPXE binary)"] == "+15 s"
    assert rows["boot.ipxe fetched"] == "+20 s"
    assert rows["hostname file fetched"] == "+20 s"
    assert rows["installer initrd"] == "+90 s" and rows["installer kernel"] == "+95 s"
    assert rows["installer ISO"] == "+150 s" and rows["autoinstall user-data"] == "+160 s"
    assert rows["boot override"] == "Pxe (redfish)"
    assert rows["hostname file removed"] == "yes"
    assert r.detail["headline"].startswith("installed: powered off")
    assert "hostname-rr101-c37.ipxe" in r.artifacts
    body = next(a for a, _ in f.calls if a[0] == "curl")
    boot = json.loads(body[body.index("-d") + 1])["Boot"]
    assert boot == {"BootSourceOverrideEnabled": "Continuous",
                    "BootSourceOverrideTarget": "Pxe", "BootSourceOverrideMode": "UEFI"}
    _no_pw_in_argv(f)


def test_install_from_off_powers_on(tmp_path, pxe, iso):
    f = Fake(tmp_path, power="off")
    r = run_install(f, pxe, iso)
    assert r.verdict == "pass" and f.action == "on"


def test_cycle_blip_off_is_not_an_install(tmp_path, pxe, iso):
    # the first two reads land in the cycle's off-blip (t=0, t=30)
    f = Fake(tmp_path, blip=45.0)
    r = run_install(f, pxe, iso)
    assert r.verdict == "pass" and r.detail["off_after_s"] >= 1500


def test_hostname_file_removed_once_installer_fetched(tmp_path, pxe, iso):
    seen = []
    f = Fake(tmp_path, off_at=None)
    orig = f._power

    def power():
        if f.acted is not None:
            seen.append((f.t[0], pxeboot.exists(NAME, pxe_dir=str(pxe))))
        return orig()
    f._power = power
    run_install(f, pxe, iso, deadline_s=600)
    # present until the kernel fetch (+95 s) was read, gone from then on
    assert seen and all(e for t, e in seen if t < 90)
    assert not any(e for t, e in seen if t >= 120)


def test_never_powers_off_fails_at_deadline_with_sol_tail(tmp_path, pxe, iso):
    sol = tmp_path / "sol.txt"
    sol.write_text("boot\n" * 1000 + "Please select your keyboard layout\r\n")
    f = Fake(tmp_path, off_at=None)
    r = run_install(f, pxe, iso, sol=sol, deadline_s=3600)
    assert r.verdict == "fail"
    assert "never powered off within 3600 s" in r.detail["error"]
    assert "installer running" in r.detail["error"]
    assert r.detail["sol_tail"].endswith("Please select your keyboard layout\n")
    assert len(r.detail["sol_tail"]) <= install.SOL_TAIL
    assert not pxeboot.exists(NAME, pxe_dir=str(pxe))
    assert f.t[0] <= 3600


def test_off_without_installer_fetch_fails_early(tmp_path, pxe, iso):
    f = Fake(tmp_path, power="off", action_takes=False)
    r = run_install(f, pxe, iso)
    assert r.verdict == "fail" and "without fetching the installer" in r.detail["error"]
    assert f.t[0] < 120
    assert not pxeboot.exists(NAME, pxe_dir=str(pxe))


def test_404_installer_fetch_is_not_a_fetch(tmp_path, pxe, iso):
    f = Fake(tmp_path, fetch_code=404, off_at=600)
    r = run_install(f, pxe, iso)
    assert r.verdict == "fail" and "without fetching the installer" in r.detail["error"]
    assert _rows(r)["installer kernel"] == "not seen"


def test_power_unknown_is_unmeasured_and_touches_nothing(tmp_path, pxe, iso):
    f = Fake(tmp_path, power=None)
    r = run_install(f, pxe, iso)
    assert r.verdict == "unmeasured" and "not acting" in r.detail["headline"]
    assert not any(a[0] == "curl" for a, _ in f.calls)
    assert os.listdir(pxe) == ["templates"]


def test_preflight_missing_installer(tmp_path, pxe, iso):
    (iso / "ga/casper/vmlinuz").unlink()
    f = Fake(tmp_path)
    r = run_install(f, pxe, iso)
    assert r.verdict == "fail" and "mountisos" in r.detail["error"]
    assert f.calls == []


def test_override_failure_fails_and_removes_file(tmp_path, pxe, iso):
    f = Fake(tmp_path, http="401")
    orig = f.__call__

    def runner(argv, **kw):
        if argv[0] == "ipmitool" and "bootdev" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="nope")
        return orig(argv, **kw)
    r = install.install(BMC, HOST, NAME, creds(), pxe_dir=str(pxe), install_root=str(iso),
                        dnsmasq=str(f.dnsmasq), nginx=str(f.nginx), runner=runner,
                        sleep=f.sleep, clock=f.clock, wall=f.wall)
    assert r.verdict == "fail" and r.detail["error"].startswith("boot override")
    assert f.acted is None
    assert not pxeboot.exists(NAME, pxe_dir=str(pxe))


def test_file_not_removed_fails_even_after_install(tmp_path, pxe, iso, monkeypatch):
    monkeypatch.setattr(install.pxeboot, "remove", lambda *a, **k: (False, "EPERM"))
    f = Fake(tmp_path)
    r = run_install(f, pxe, iso)
    assert r.verdict == "fail"
    assert "hostname file NOT removed (EPERM)" in r.detail["error"]
    assert r.detail["error"].startswith("hostname file")       # the pass headline is not kept
    assert _rows(r)["hostname file removed"] == "NO (EPERM)"


def test_exception_mid_wait_still_removes_file(tmp_path, pxe, iso, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(install, "install_marks", boom)
    f = Fake(tmp_path)
    with pytest.raises(RuntimeError):
        run_install(f, pxe, iso)
    assert not pxeboot.exists(NAME, pxe_dir=str(pxe))


def test_bmc_gone_at_deadline_is_unmeasured(tmp_path, pxe, iso):
    f = Fake(tmp_path, off_at=None)
    orig = f._power

    def power():
        if f.acted is not None and f.t[0] - f.acted > 300:
            f.power0 = None
        return orig()
    f._power = power
    r = run_install(f, pxe, iso, deadline_s=900)
    assert r.verdict == "unmeasured" and "BMC stopped answering" in r.detail["headline"]


def test_marks_survive_log_rotation(tmp_path, pxe, iso):
    f = Fake(tmp_path, off_at=600)
    orig = f._power

    def power():
        if f.acted is not None and f.t[0] - f.acted > 300 and f.nginx.stat().st_size:
            f.nginx.write_text("")            # copytruncate at 00:00
        return orig()
    f._power = power
    r = run_install(f, pxe, iso)
    assert r.verdict == "pass" and _rows(r)["installer kernel"] == "+95 s"


def test_install_marks_matches_only_this_host(tmp_path):
    log = tmp_path / "access.log"
    log.write_text(_nginx("10.9.0.55", BASE + 5, "/install//canonical/ubuntu/24.04/ga/casper/vmlinuz") +
                   _nginx(HOST, BASE + 6, "/v32/ipxe/hostname-rr101-c37.ipxe") +
                   _nginx(HOST, BASE + 7, "/ipxe/hostname-rr101-c370.ipxe"))
    m = install.install_marks(HOST, NAME, BASE, dnsmasq=os.devnull, nginx=str(log))
    assert m["kernel"] == [] and m["hostname.ipxe"] == [BASE + 6]


# ----------------------------------------------------------------- firstboot
def run_fb(fake, pxe, **kw):
    kw.setdefault("deadline_s", 1200)
    return install.firstboot(BMC, HOST, NAME, kw.pop("creds", creds()), pxe_dir=str(pxe),
                             runner=fake, sleep=fake.sleep, clock=fake.clock, **kw)


def test_firstboot_happy_path(tmp_path, pxe):
    pxeboot.write(NAME, pxe_dir=str(pxe))
    f = Fake(tmp_path, power="off", probe=probe())
    r = run_fb(f, pxe, expect_mac=MAC)
    assert r.verdict == "pass", r.detail
    assert not pxeboot.exists(NAME, pxe_dir=str(pxe))
    assert f.action == "on"
    body = next(a for a, _ in f.calls if a[0] == "curl")
    boot = json.loads(body[body.index("-d") + 1])["Boot"]
    assert boot == {"BootSourceOverrideEnabled": "Continuous",
                    "BootSourceOverrideTarget": "Hdd", "BootSourceOverrideMode": "UEFI"}
    rows = _rows(r)
    assert rows["VERSION_ID"] == "24.04" and rows["root disk"] == "nvme0n1"
    assert rows["boot override"] == "Hdd (redfish)" and rows["ssh user"] == "flax"
    assert r.detail["headline"] == "Ubuntu 24.04 up as flax after 120 s, / on nvme0n1"
    _no_pw_in_argv(f)


def test_firstboot_ipmi_fallback_is_bootdev_disk(tmp_path, pxe):
    f = Fake(tmp_path, power="off", http="500", probe=probe())
    r = run_fb(f, pxe)
    assert r.verdict == "pass" and _rows(r)["boot override"] == "Hdd (ipmitool)"
    bd = [a for a, _ in f.calls if "bootdev" in a]
    assert bd and bd[0][-3:] == ["bootdev", "disk", "options=efiboot,persistent"]


def test_firstboot_auth_refused_names_user_and_tries_no_other(tmp_path, pxe):
    f = Fake(tmp_path, power="off", ssh_rc=5, probe=probe())
    r = run_fb(f, pxe, deadline_s=1200)
    assert r.verdict == "fail"
    assert "ssh as flax@10.9.0.5 refused" in r.detail["error"]
    users = {x.split("@")[0] for a, _ in f.calls if a[0] == "sshpass" for x in a if "@" in x}
    assert users == {"flax"}
    # fails after AUTH_FAIL_AFTER refusals in a row, not at the 1200 s deadline
    ssh = [a for a, _ in f.calls if a[0] == "sshpass"]
    assert len(ssh) == install.AUTH_FAIL_AFTER and f.t[0] < 60


def test_firstboot_probe_runs_without_sudo(tmp_path, pxe):
    f = Fake(tmp_path, power="off", probe=probe())
    assert run_fb(f, pxe).verdict == "pass"
    ssh = [a for a, k in f.calls if a[0] == "sshpass" and "boot_id=" in (k.get("input") or "")]
    assert ssh and all("sudo" not in a and a[-2:] == ["bash", "-s"] for a in ssh)


def test_firstboot_no_answer_fails_at_deadline(tmp_path, pxe):
    sol = tmp_path / "sol.txt"
    sol.write_text("grub rescue>\n")
    f = Fake(tmp_path, power="off", up_after=99999, probe=probe())
    r = run_fb(f, pxe, deadline_s=600, sol_path=sol)
    assert r.verdict == "fail"
    assert r.detail["error"] == "no ssh as flax@10.9.0.5 to a fresh boot within 600 s"
    assert r.detail["sol_tail"] == "grub rescue>\n"
    assert r.detail["last"]["power"] == "on"


@pytest.mark.parametrize("kw,err", [
    ({"cmdline": LIVE}, "booted the live image"),
    ({"version": "22.04"}, "VERSION_ID 22.04 != 24.04"),
    ({"root": "overlay", "blk": ""}, "/ is overlay, not a disk"),
    ({"blk": 'blk=NAME="sda1" TYPE="part" TRAN="" RM="0"\n'
             'blk=NAME="sda" TYPE="disk" TRAN="usb" RM="1"\n', "root": "/dev/sda1"},
     "removable/USB disk sda"),
    ({"macs": ("0c:42:a1:00:00:01",)}, "wrong host"),
])
def test_firstboot_installed_os_proof(tmp_path, pxe, kw, err):
    # the plain proof, outside the BootOrder-recovery families (recovery: tests below)
    f = Fake(tmp_path, power="off", probe=probe(**kw))
    r = run_fb(f, pxe, expect_mac=MAC, family="other")
    assert r.verdict == "fail" and err in r.detail["error"], r.detail


def test_firstboot_rerun_waits_past_the_old_os(tmp_path, pxe):
    old = probe(boot_id=OLD)
    f = Fake(tmp_path, power="on", probe=probe(), old_probe=lambda t: old(86400 + t))
    r = run_fb(f, pxe)
    assert r.verdict == "pass" and f.action == "cycle"
    assert r.detail["old_boot_id"] == OLD and r.detail["boot_id"] == NEW


def test_firstboot_same_boot_id_never_passes(tmp_path, pxe):
    # the host keeps answering with the pre-action boot id: never judged
    f = Fake(tmp_path, power="on", probe=probe(boot_id=OLD),
             old_probe=lambda t: probe(boot_id=OLD)(86400 + t))
    r = run_fb(f, pxe, deadline_s=300)
    assert r.verdict == "fail" and "fresh boot" in r.detail["error"]


def test_firstboot_power_unknown_is_unmeasured(tmp_path, pxe):
    f = Fake(tmp_path, power=None, probe=probe())
    r = run_fb(f, pxe)
    assert r.verdict == "unmeasured"
    assert not any(a[0] == "curl" for a, _ in f.calls)


def test_firstboot_needs_host_creds(tmp_path, pxe):
    f = Fake(tmp_path, power="off", probe=probe())
    r = run_fb(f, pxe, creds=creds(host=()))
    assert r.verdict == "fail" and "credentials-host.json" in r.detail["error"]
    assert f.calls == []


def test_firstboot_refuses_when_file_cannot_be_removed(tmp_path, pxe, monkeypatch):
    monkeypatch.setattr(install.pxeboot, "remove", lambda *a, **k: (False, "EPERM"))
    f = Fake(tmp_path, power="off", probe=probe())
    r = run_fb(f, pxe)
    assert r.verdict == "fail" and "would reinstall" in r.detail["error"]
    assert f.calls == []


def test_parse_probe_and_root_disk():
    p = install.parse_probe(probe()(12.5))
    assert p["uptime"] == 12.5 and p["version_id"] == "24.04"
    assert p["macs"][0] == "b8599f000001"
    assert install.root_disk(p) == ("nvme0n1", None)
    assert install.parse_probe("version_id=\n")["version_id"] is None


# ---- firstboot BootOrder recovery (spec 2026-11-11-deploy-firstboot-boot-order §2-§3) ----
from test_flax_rungs_bootorder import ESP, ET7B4   # noqa: E402

NEW2 = "33333333-3333-3333-3333-333333333333"


def live_fake(tmp_path, **kw):
    kw.setdefault("after", probe(boot_id=NEW2))
    kw.setdefault("partuuids", (ESP, "deadbeef-0000-0000-0000-000000000000"))
    return Fake(tmp_path, power="off", probe=probe(cmdline=LIVE), efi=ET7B4, **kw)


def _efi_scripts(f):
    return [s for s in f.scripts if "efibootmgr" in s]


def test_firstboot_live_image_fixes_order_and_reboots(tmp_path, pxe):
    f = live_fake(tmp_path)
    r = run_fb(f, pxe, expect_mac=MAC)
    assert r.verdict == "pass", r.detail
    rows = _rows(r)
    assert rows["boot order fix"] == "written 0000"
    assert rows["boot order (live)"].startswith("BootCurrent: 0003 · BootOrder: 0003,")
    assert rows["boot order (installed)"] == "BootCurrent: 0000 · BootOrder: 0000,0003"
    assert f.writes == ["0000,0003,0006,0007,0008,0009,000C,000B,000A"]
    assert sum("systemctl reboot" in s for s in f.scripts) == 1
    assert [a[-1] for a, _ in f.calls if a[0] == "ipmitool" and a[-1] in ("on", "cycle")] == ["on"]
    assert r.detail["boot_id"] == NEW2


def test_firstboot_live_image_no_entry_on_host_fails(tmp_path, pxe):
    f = live_fake(tmp_path, partuuids=("aaaaaaaa-0000-0000-0000-000000000000",))
    r = run_fb(f, pxe)
    assert r.verdict == "fail" and "no ubuntu boot entry" in r.detail["error"]
    assert f.writes == [] and f.rebooted is None


def test_firstboot_readback_mismatch_fails(tmp_path, pxe):
    f = live_fake(tmp_path, readback_ok=False)
    r = run_fb(f, pxe)
    assert r.verdict == "fail" and "did not take" in r.detail["error"]
    assert f.rebooted is None


def test_firstboot_live_again_after_fix_fails(tmp_path, pxe):
    f = live_fake(tmp_path, after=probe(boot_id=NEW2, cmdline=LIVE))
    r = run_fb(f, pxe)
    assert r.verdict == "fail"
    assert "live image" in r.detail["error"] and "after a BootOrder fix" in r.detail["error"]
    assert len(f.writes) == 1 and r.detail["boot_id"] == NEW2


def test_firstboot_leopard_live_image_is_todays_fail(tmp_path, pxe):
    f = live_fake(tmp_path)
    r = run_fb(f, pxe, family="leopard")
    assert r.verdict == "fail" and "booted the live image" in r.detail["error"]
    assert "after a BootOrder fix" not in r.detail["error"]
    assert _rows(r)["boot order fix"] == "not tried (leopard)"
    assert _efi_scripts(f) == []


def test_firstboot_live_sudo_refused_names_sudo(tmp_path, pxe):
    f = live_fake(tmp_path, sudo_ok=False)
    r = run_fb(f, pxe)
    assert r.verdict == "fail" and "sudo -n refused" in r.detail["error"]
    assert f.writes == []


def test_firstboot_happy_path_boot_order_rows(tmp_path, pxe):
    f = Fake(tmp_path, power="off", probe=probe())
    rows = _rows(run_fb(f, pxe))
    assert rows["boot order fix"] == "not needed" and rows["boot order (live)"] == "not seen"
    assert rows["boot order (installed)"] == "BootCurrent: 0000 · BootOrder: 0000,0003"


def test_firstboot_installed_order_unreadable_still_passes(tmp_path, pxe):
    f = Fake(tmp_path, power="off", probe=probe(), installed_sudo_ok=False)
    r = run_fb(f, pxe)
    assert r.verdict == "pass" and _rows(r)["boot order (installed)"] == "not read"
