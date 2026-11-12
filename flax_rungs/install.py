"""Ubuntu 24.04 install + first boot of the installed OS (donum 69/72 without their
nic0.txt / bmcs.txt / 00_slack.txt).

install(): the node is in the live image (or off). The caller starts the SOL capture
first (sol.start; it spans install and firstboot, then solscan.scan_file).
  1. preflight: the installer kernel/initrd (the mounted ISO) and the wiwynn-tp
     autoinstall are on disk, else nothing is touched ("run mountisos");
  2. pxeboot.write hostname-<hostname>.ipxe (menu-default ubuntu, C_24.04);
  3. Redfish Pxe/Continuous/UEFI + chassis power cycle (on, when off);
  4. the autoinstall ends `shutdown -h now`: wait for chassis power OFF. An "off"
     counts only after nginx logged this host fetching the installer's initrd and
     kernel since the power action (a cycle's own off-blip comes before them);
     three "off" reads without them fail early (the node never ran the installer).
     The bootlog marks (tftp, boot.ipxe, the hostname file, initrd, kernel, ISO,
     user-data) are re-read every poll and kept, so a log rotation mid-install
     cannot lose them; each is an evidence row.
  The hostname file is removed as soon as the installer kernel was fetched, and
  always on the way out (pass, fail, deadline, exception): a later power-on of a
  node whose override is still Pxe must never reinstall. A file that cannot be
  removed fails the rung. A deadline failure carries the SOL tail.

firstboot(): remove the hostname file (again), Redfish Hdd/Continuous/UEFI (ghost
setbootdisk's body), power on (cycle when on), then ssh as flax (credentials-host.json,
nothing else is tried) until a FRESH boot answers (uptime shorter than the time since
the power action; a boot_id read before the action must change), and prove the
installed OS: /etc/os-release VERSION_ID="24.04", /proc/cmdline without BOOTIF=, / on
a /dev block device whose disk is not USB/removable, and (when given) the host MAC
Discover matched among its interfaces. The probe needs no root, so it runs as flax
without sudo. A login refused on AUTH_FAIL_AFTER polls in a row fails at once, naming
the user, instead of waiting out the deadline (in a row: a cycled node's old OS may
still answer the first poll).
A fresh boot that is the live image (the BIOS's FixedBootOrder kept the network ahead of
the new ubuntu entry) gets ONE recovery on BOOTORDER_FIX_FAMILIES: ubuntu entry first via
efibootmgr (sudo -n), an OS reboot, the proof again. The rows carry the order the live
image saw, the fix, and the installed OS's order (spec 2026-11-11-deploy-firstboot)."""
import re
import subprocess
import time
from pathlib import Path

from flax_post.observe import bootlog

from . import bmc, bootorder, hostssh, pxeboot, redfish, solscan
from .result import UNMEASURED, RungResult

INSTALL_ROOT = "/export/install/canonical/ubuntu/24.04"
INSTALLER = ("ga/casper/initrd", "ga/casper/vmlinuz")
AUTOINSTALL = "wiwynn-tp/user-data"
VERSION_ID = "24.04"
OFF_WITHOUT_INSTALL = 3        # consecutive "off" reads before the installer ran -> fail
AUTH_FAIL_AFTER = 3            # consecutive refused logins at first boot -> fail
SOL_TAIL = 2000
# families whose BIOS keeps an OS BootOrder write (AMI FixedBootOrder TiogaPass): firstboot
# may fix the order from the live image once and reboot (spec 2026-11-11 §2). Leopard
# BIOSes ignore efibootmgr -o, so they keep the plain fail.
BOOTORDER_FIX_FAMILIES = frozenset({"tiogapass"})
_EFI_READ = "efibootmgr -v; echo ---; lsblk -nro PARTUUID\n"
_REBOOT = "nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 &\n"

_OK = r' HTTP/[\d.]+" (?:200|206) '
_NGINX = {      # mark -> request path (status 200/206 only: a 404 is not a fetch)
    "initrd": r"/install/+canonical/ubuntu/24\.04/ga/casper/initrd",
    "kernel": r"/install/+canonical/ubuntu/24\.04/ga/casper/vmlinuz",
    "iso": r"/isos/+canonical/ubuntu/24\.04/[^ \"]+\.iso",
    "user-data": r"/install/+canonical/ubuntu/24\.04/[\w.-]+/user-data",
}
_LABELS = (("tftp", "tftp (iPXE binary)"), ("boot.ipxe", "boot.ipxe fetched"),
           ("hostname.ipxe", "hostname file fetched"), ("initrd", "installer initrd"),
           ("kernel", "installer kernel"), ("iso", "installer ISO"),
           ("user-data", "autoinstall user-data"))


def preflight(install_root: str = INSTALL_ROOT, autoinstall: str = AUTOINSTALL) -> str | None:
    """None when the installer and autoinstall are served, else what is missing."""
    for rel in (*INSTALLER, autoinstall):
        p = Path(install_root) / rel
        if not p.is_file():
            hint = " (ISO not mounted? run mountisos)" if rel in INSTALLER else ""
            return f"{p} missing{hint}"
    return None


def _req(host_ip: str, path_rx: str):
    rx = re.compile(r'"GET ' + path_rx + _OK)
    return lambda line: line.startswith(host_ip + " ") and rx.search(line) is not None


def install_marks(host_ip: str, hostname: str, since: float, *, dnsmasq: str | None = None,
                  nginx: str | None = None) -> dict:
    """{mark: [epoch, ...]} at or after `since` for this host: bootlog's tftp + boot.ipxe,
    and nginx fetches of the hostname file and the installer."""
    dn = dnsmasq or bootlog.DNSMASQ_LOG
    ng = nginx or bootlog.NGINX_ACCESS_LOG
    t = bootlog.boot_times(dn, ng, host_ip, since)
    out = {"tftp": t["tftp"], "boot.ipxe": t["ipxe"]}
    reqs = dict(_NGINX)
    reqs["hostname.ipxe"] = r"/(?:v\d+/)?ipxe/hostname-" + re.escape(hostname) + r"\.ipxe"
    for name, rx in reqs.items():
        # bootlog's bounded newest-first tail read, with this rung's matchers
        out[name] = bootlog._collect(ng, since, _req(host_ip, rx), bootlog._nginx_ts,
                                     bootlog._MAX_BYTES)
    return out


def _merge(marks: dict, new: dict):
    """Keep each mark's earliest time across polls."""
    for k, v in new.items():
        if v:
            marks[k] = min(v) if k not in marks else min(marks[k], min(v))


def _mark_rows(marks: dict, t0: float) -> list[list[str]]:
    return [[label, f"+{int(marks[k] - t0)} s" if k in marks else "not seen"]
            for k, label in _LABELS]


def _tail(sol_path) -> str:
    return solscan.tail(sol_path, SOL_TAIL) if sol_path else ""


def install(bmc_ip: str, host_ip: str, hostname: str, creds, *, sol_path=None,
            deadline_s: int = 3600, poll_s: int = 30, pxe_dir: str = pxeboot.PXE_DIR,
            install_root: str = INSTALL_ROOT, dnsmasq: str | None = None,
            nginx: str | None = None, runner=subprocess.run, sleep=time.sleep,
            clock=time.monotonic, wall=time.time, platform: str = pxeboot.PLATFORM,
            autoinstall: str = AUTOINSTALL, bootdev_persistent: bool = True) -> RungResult:
    """platform: the iPXE templates/<platform>.part (its node-setting picks the
    autoinstall dir); autoinstall: that dir's user-data, checked by preflight. The
    defaults are rackpost's Tioga Pass pair."""
    detail = {"override": None, "power_action": None, "file": None, "file_removed": None,
              "off_after_s": None, "marks": {}, "cipher": None, "platform": platform}
    missing = preflight(install_root, autoinstall)
    if missing:
        return _done(detail, "fail", f"installer not served: {missing}", [], [])
    info: dict = {}
    state = bmc.power_status(bmc_ip, creds.bmc, info=info, runner=runner)
    detail["cipher"] = info.get("cipher")
    if state is None:
        why = "power state unknown (no IPMI answer on any credential/cipher); not acting"
        return _done(detail, UNMEASURED, why, [], [])
    w = pxeboot.write(hostname, platform=platform, pxe_dir=pxe_dir)
    if w.verdict != "pass":
        return _done(detail, "fail", f"hostname iPXE file: {w.detail.get('error')}", [], [])
    detail["file"] = w.detail["path"]
    arts = dict(w.artifacts)
    marks: dict = {}
    t0w = wall()
    try:
        res = _install_wait(detail, marks, bmc_ip, host_ip, hostname, creds, state,
                            persistent=bootdev_persistent,
                            deadline_s=deadline_s, poll_s=poll_s, pxe_dir=pxe_dir,
                            dnsmasq=dnsmasq, nginx=nginx, runner=runner, sleep=sleep,
                            clock=clock, wall=wall)
    finally:
        gone, err = pxeboot.remove(hostname, pxe_dir=pxe_dir)
        if detail["file_removed"] is None or not gone:
            detail["file_removed"] = gone
    verdict, why = res
    detail["marks"] = dict(marks)
    rows = [["hostname file", detail["file"]],
            ["boot override", detail["override"] or "not set"],
            ["power action", detail["power_action"] or "none"],
            *_mark_rows(marks, t0w),
            ["powered off", f"+{detail['off_after_s']} s" if detail["off_after_s"] is not None
             else "no"],
            ["hostname file removed", "yes" if detail["file_removed"] else f"NO ({err})"]]
    notes = ["the autoinstall ends `shutdown -h now`: power off after the installer "
             "fetches = installed"]
    if not detail["file_removed"]:
        why = (f"{why}; " if verdict != "pass" else "") + \
            f"hostname file NOT removed ({err}): a power-on would reinstall"
        verdict = "fail"
    if verdict != "pass":
        detail["sol_tail"] = _tail(sol_path)
    return _done(detail, verdict, why, rows, notes, arts)


def _done(detail, verdict, why, rows, notes, arts=None) -> RungResult:
    d = {**detail, "headline": why, "rows": rows, "notes": notes}
    if verdict == "fail":
        d["error"] = why
    elif verdict == UNMEASURED:
        d.update(error=why, reason=why)
    return RungResult(verdict, d, arts or {})


def _install_wait(detail, marks, bmc_ip, host_ip, hostname, creds, state, *, deadline_s,
                  poll_s, pxe_dir, dnsmasq, nginx, runner, sleep, clock, wall, persistent=True):
    """(verdict, headline) of steps 3-4; fills detail/marks as it goes."""
    ov = redfish.set_boot_pxe(bmc_ip, creds.redfish, ipmi_creds=creds.bmc, runner=runner,
                              persistent=persistent)
    if ov.verdict != "pass":
        return "fail", f"boot override: {ov.detail.get('error')}"
    detail["override"] = f"Pxe ({ov.detail.get('method')})"
    action = "cycle" if state == "on" else "on"
    t0, t0w = clock(), wall()           # before the call: the action may land mid-call
    if not bmc.power_action(bmc_ip, creds.bmc, action, runner=runner):
        detail["power_action"] = f"{action} FAILED"
        return "fail", f"chassis power {action} failed"
    detail["power_action"] = action
    end = t0 + deadline_s
    off_early = 0
    last = None
    while True:
        try:
            _merge(marks, install_marks(host_ip, hostname, t0w, dnsmasq=dnsmasq, nginx=nginx))
        except OSError:
            pass
        installer = "initrd" in marks and "kernel" in marks
        if installer and not detail["file_removed"]:
            # the installer is loaded: the file has done its job
            detail["file_removed"] = pxeboot.remove(hostname, pxe_dir=pxe_dir)[0] or None
        last = bmc.power_status(bmc_ip, creds.bmc, runner=runner)
        if last == "off":
            if installer:
                detail["off_after_s"] = int(clock() - t0)
                return "pass", f"installed: powered off {detail['off_after_s']} s after " \
                               f"power {action}"
            off_early += 1
            if off_early >= OFF_WITHOUT_INSTALL:
                return "fail", ("powered off without fetching the installer (no initrd/"
                                f"kernel GET from {host_ip} since power {action})")
        elif last == "on":
            off_early = 0
        if clock() + poll_s >= end:
            break
        sleep(poll_s)
    if last is None:
        return UNMEASURED, (f"BMC stopped answering; install outcome not measured "
                            f"within {deadline_s} s")
    stage = ("installer running" if "kernel" in marks else
             "installer never fetched" if not marks else "booted, installer not fetched")
    return "fail", (f"never powered off within {deadline_s} s ({stage}; installer hung or "
                    "waiting for input? see the SOL tail)")


# ---------------------------------------------------------------- first boot
_PROBE = r"""echo "boot_id=$(cat /proc/sys/kernel/random/boot_id)"
echo "uptime=$(cut -d' ' -f1 /proc/uptime)"
echo "cmdline=$(cat /proc/cmdline)"
echo "version_id=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"')"
echo "hostname=$(hostname)"
src=$(findmnt -no SOURCE /)
echo "root=$src"
echo "macs=$(cat /sys/class/net/*/address 2>/dev/null | tr '\n' ' ')"
lsblk -snPo NAME,TYPE,TRAN,RM "$src" 2>/dev/null | sed 's/^/blk=/'
"""
_BLK = re.compile(r'(\w+)="([^"]*)"')
_AUTH = re.compile(r"Permission denied|incorrect password|Authentication failed", re.I)


def parse_probe(out: str) -> dict:
    """The _PROBE output -> {boot_id, uptime (float|None), cmdline, version_id, hostname,
    root, macs [hex12], blk [{NAME, TYPE, TRAN, RM}]}."""
    d = {"boot_id": None, "uptime": None, "cmdline": "", "version_id": None,
         "hostname": None, "root": None, "macs": [], "blk": []}
    for line in out.splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        v = v.strip()
        if k == "blk":
            d["blk"].append(dict(_BLK.findall(v)))
        elif k == "uptime":
            try:
                d["uptime"] = float(v)
            except ValueError:
                pass
        elif k == "macs":
            d["macs"] = [re.sub(r"[^0-9a-f]", "", m.lower()) for m in v.split()]
        elif k == "cmdline":
            d[k] = v
        elif k in d:
            d[k] = v or None
    return d


def root_disk(p: dict) -> tuple[str | None, str | None]:
    """(disk name, None) when / is on a /dev block device whose disk is internal, else
    (None, why not)."""
    src = p.get("root") or ""
    if not src.startswith("/dev/"):
        return None, f"/ is {src or 'unreadable'}, not a disk (live image?)"
    disks = [b for b in p.get("blk") or [] if b.get("TYPE") == "disk"]
    if not disks:
        return None, f"no disk under {src}"
    for b in disks:
        if (b.get("TRAN") or "").lower() == "usb" or b.get("RM") == "1":
            return None, f"/ on removable/USB disk {b.get('NAME')}"
    return disks[0].get("NAME"), None


def fix_boot_order(host_ip: str, user: str, pw: str, *, runner=subprocess.run
                   ) -> tuple[bool, str, str]:
    """From the live image (sudo -n): put this host's ubuntu entry first in BootOrder and
    read it back. (ok, fix row, the live image's order line)."""
    rc, out, err = hostssh.run(host_ip, user, pw, _EFI_READ, timeout=30, sudo=True, runner=runner)
    if rc != 0:
        if re.search(r"sudo|password", err or "", re.I):
            return False, "sudo -n refused on the live image", "not read"
        return False, f"efibootmgr not read on the live image ({hostssh.tail(out, err, 120)})", \
            "not read"
    efi, _, parts = out.partition("---\n")
    live = bootorder.order_line(efi)
    p = bootorder.parse(efi)
    entry = bootorder.ubuntu_entry(p["entries"], set(parts.split()))
    if entry is None:
        return False, ("no ubuntu boot entry on this host's disks: the install did not "
                       "register one"), live
    new = ",".join(bootorder.ubuntu_first(p["order"], entry))
    rc, out, err = hostssh.run(host_ip, user, pw, f"efibootmgr -o {new}\n", timeout=30,
                               sudo=True, runner=runner)
    got = bootorder.parse(out)["order"] if rc == 0 else []
    if not got or got[0] != entry:
        return False, f"BootOrder write did not take ({bootorder.order_line(out)})", live
    return True, f"written {entry}", live


def _installed_order(host_ip, user, pw, runner) -> str:
    rc, out, _ = hostssh.run(host_ip, user, pw, "efibootmgr\n", timeout=20, sudo=True,
                             runner=runner)
    return bootorder.order_line(out) if rc == 0 else "not read"


def firstboot(bmc_ip: str, host_ip: str, hostname: str, creds, *,
              expect_mac: str | None = None, sol_path=None, deadline_s: int = 1200,
              poll_s: int = 15, pxe_dir: str = pxeboot.PXE_DIR, runner=subprocess.run,
              sleep=time.sleep, clock=time.monotonic, bootdev_persistent: bool = True,
              family: str = "tiogapass") -> RungResult:
    detail = {"override": None, "power_action": None, "ssh_user": None, "ssh_after_s": None,
              "old_boot_id": None, "boot_id": None, "uptime_s": None, "version_id": None,
              "root": None, "disk": None, "hostname": None, "cipher": None,
              "bootorder_live": "not seen", "bootorder_fix": "not needed",
              "bootorder_installed": "not read"}
    if not creds.host:
        return _done(detail, "fail", "add credentials-host.json (flax row)", [], [])
    user, pw = creds.host
    detail["ssh_user"] = user
    gone, err = pxeboot.remove(hostname, pxe_dir=pxe_dir)
    if not gone:
        return _done(detail, "fail", f"hostname file NOT removed ({err}): not booting a "
                                     "node that would reinstall", [], [])
    info: dict = {}
    state = bmc.power_status(bmc_ip, creds.bmc, info=info, runner=runner)
    detail["cipher"] = info.get("cipher")
    if state is None:
        why = "power state unknown (no IPMI answer on any credential/cipher); not acting"
        return _done(detail, UNMEASURED, why, [], [])
    ov = redfish.set_boot_disk(bmc_ip, creds.redfish, ipmi_creds=creds.bmc, runner=runner,
                               persistent=bootdev_persistent)
    if ov.verdict != "pass":
        return _done(detail, "fail", f"Hdd boot override: {ov.detail.get('error')}", [], [])
    detail["override"] = f"Hdd ({ov.detail.get('method')})"
    if state == "on":                   # a re-run: whatever runs now must not be judged
        rc, out, _ = hostssh.run(host_ip, user, pw, _PROBE, timeout=20, sudo=False,
                                 runner=runner)
        if rc == 0:
            detail["old_boot_id"] = parse_probe(out)["boot_id"]
    action = "cycle" if state == "on" else "on"
    t0 = clock()
    if not bmc.power_action(bmc_ip, creds.bmc, action, runner=runner):
        detail["power_action"] = f"{action} FAILED"
        return _fb_done(detail, "fail", f"chassis power {action} failed", sol_path)
    detail["power_action"] = action
    end = t0 + deadline_s
    last_rc, last_err, refused = None, "", 0
    while True:
        rc, out, err = hostssh.run(host_ip, user, pw, _PROBE, timeout=20, sudo=False,
                                   runner=runner)
        if rc == 0:
            p = parse_probe(out)
            since = clock() - t0
            stale = (p["uptime"] is None or p["uptime"] >= since or
                     (detail["old_boot_id"] and p["boot_id"] == detail["old_boot_id"]))
            if not stale:
                detail.update(boot_id=p["boot_id"], uptime_s=int(p["uptime"]),
                              ssh_after_s=int(since))
                live = "BOOTIF=" in p["cmdline"]
                if live and detail["bootorder_fix"] == "not needed":
                    if family not in BOOTORDER_FIX_FAMILIES:
                        detail["bootorder_fix"] = f"not tried ({family})"
                    else:
                        ok, row, line = fix_boot_order(host_ip, user, pw, runner=runner)
                        detail.update(bootorder_fix=row, bootorder_live=line)
                        if not ok:
                            detail.update(cmdline=p["cmdline"], version_id=p["version_id"],
                                          root=p["root"], hostname=p["hostname"])
                            return _fb_done(detail, "fail", row, sol_path)
                        hostssh.run(host_ip, user, pw, _REBOOT, timeout=20, sudo=True,
                                    runner=runner)
                        # the live boot is now the old one; freshness counts from the reboot
                        detail["old_boot_id"] = p["boot_id"]
                        t0 = clock()
                        if clock() + poll_s >= end:
                            break
                        sleep(poll_s)
                        continue
                if not live:
                    detail["bootorder_installed"] = _installed_order(host_ip, user, pw, runner)
                return _judge(detail, p, host_ip, expect_mac, sol_path)
            last_rc, last_err, refused = 0, "old OS still answering", 0
        else:
            last_rc, last_err = rc, (err or "").strip()[-160:]
            # sshpass exits 5 on a refused password
            auth = rc == 5 or bool(_AUTH.search(err or ""))
            refused = refused + 1 if auth else 0
            if refused >= AUTH_FAIL_AFTER:
                break
        if clock() + poll_s >= end:
            break
        sleep(poll_s)
    detail["last"] = {"ssh": f"rc={last_rc} {last_err}".strip(),
                      "power": bmc.power_status(bmc_ip, creds.bmc, runner=runner),
                      "ping": bmc.ping(host_ip, runner=runner)}
    if refused:
        why = (f"ssh as {user}@{host_ip} refused (password; the autoinstall's users differ "
               f"from credentials-host.json, or ssh allow-pw off?); no other user tried")
    else:
        why = f"no ssh as {user}@{host_ip} to a fresh boot within {deadline_s} s"
    return _fb_done(detail, "fail", why, sol_path)


def _fb_rows(detail) -> list[list[str]]:
    return [["boot override", detail["override"] or "not set"],
            ["power action", detail["power_action"] or "none"],
            ["ssh user", detail["ssh_user"] or ""],
            ["ssh after", f"{detail['ssh_after_s']} s" if detail["ssh_after_s"] is not None
             else "no answer"],
            ["VERSION_ID", detail["version_id"] or "not read"],
            ["cmdline", detail.get("cmdline") or "not read"],
            ["root", detail["root"] or "not read"],
            ["root disk", detail["disk"] or "none"],
            ["hostname", detail["hostname"] or "not read"],
            ["boot order (live)", detail.get("bootorder_live") or "not seen"],
            ["boot order fix", detail.get("bootorder_fix") or "not needed"],
            ["boot order (installed)", detail.get("bootorder_installed") or "not read"]]


def _fb_done(detail, verdict, why, sol_path) -> RungResult:
    if verdict != "pass":
        detail["sol_tail"] = _tail(sol_path)
    return _done(detail, verdict, why, _fb_rows(detail),
                 ["the installed OS must answer ssh as flax after a fresh boot from disk"])


def _judge(detail, p, host_ip, expect_mac, sol_path) -> RungResult:
    detail.update(version_id=p["version_id"], root=p["root"], hostname=p["hostname"],
                  cmdline=p["cmdline"])
    if "BOOTIF=" in p["cmdline"]:
        after = " after a BootOrder fix" if (detail.get("bootorder_fix") or "").startswith(
            "written") else ""
        return _fb_done(detail, "fail", "booted the live image, not the installed OS "
                                        f"(BOOTIF= on /proc/cmdline){after}", sol_path)
    if p["version_id"] != VERSION_ID:
        return _fb_done(detail, "fail", f"VERSION_ID {p['version_id'] or 'unread'} != "
                                        f"{VERSION_ID}", sol_path)
    disk, why = root_disk(p)
    detail["disk"] = disk
    if why:
        return _fb_done(detail, "fail", why, sol_path)
    want = re.sub(r"[^0-9a-f]", "", (expect_mac or "").lower())
    if want and want not in p["macs"]:
        return _fb_done(detail, "fail", f"wrong host at {host_ip}: no interface has the "
                                        f"discovered MAC {expect_mac}", sol_path)
    return _fb_done(detail, "pass", f"Ubuntu {VERSION_ID} up as {detail['ssh_user']} after "
                                    f"{detail['ssh_after_s']} s, / on {disk}", sol_path)
