"""boot_live: force a PXE boot, power the node, prove a FRESH boot of the live image on
the device Discover matched.

`ipmitool chassis power cycle` returns before the host loses power, so the OS that
was running (an installed OS, or an earlier live image) can still answer ssh for a
few seconds afterwards. Nothing is judged on that old OS:
  - the power state must be readable (None -> fail, never a blind `power on`);
  - when the host answers ssh before the action, its boot_id is recorded and a
    fresh boot must show a different one;
  - always, /proc/uptime must be shorter than the time since the power action;
  - while either check says "old OS", polling continues (within the deadline).
Only a fresh boot's /proc/cmdline is read: it must be the live image (BOOTIF= and
pxeaddr=) and its BOOTIF MAC must be the host MAC Discover matched for this device."""
import re
import subprocess
import time

from . import bmc, hostssh, redfish
from .result import RungResult

_PROBE = ("cat /proc/sys/kernel/random/boot_id\n"
          "cut -d' ' -f1 /proc/uptime\n"
          "cat /proc/cmdline\n")
_BOOTIF = re.compile(r"(?:^|\s)BOOTIF=01-((?:[0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2})(?:\s|$)")
_BOOT_ID = re.compile(r"^[0-9a-f-]{36}$")


def _hex(mac: str | None) -> str | None:
    h = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    return h if len(h) == 12 else None


def _colon(h: str) -> str:
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def parse_probe(out: str) -> tuple[str | None, float | None, str]:
    """(boot_id, uptime seconds, cmdline) from the _PROBE output; None for unreadable."""
    lines = out.splitlines()
    bid = lines[0].strip() if lines and _BOOT_ID.match(lines[0].strip()) else None
    try:
        up = float(lines[1].strip()) if len(lines) > 1 else None
    except ValueError:
        up = None
    return bid, up, (lines[2] if len(lines) > 2 else "")


def bootif_mac(cmdline: str) -> str | None:
    m = _BOOTIF.search(cmdline or "")
    return _hex(m.group(1)) if m else None


def boot_live(bmc_ip: str, host_ip: str, creds, *, expect_mac: str | None = None,
              deadline_s: int = 900, poll_s: int = 15, runner=subprocess.run,
              sleep=time.sleep, clock=time.monotonic) -> RungResult:
    detail = {"override": None, "power_action": None, "ssh_after_s": None,
              "cmdline_ok": False, "old_boot_id": None, "boot_id": None, "uptime_s": None,
              "bootif": None, "cipher": None}
    want = _hex(expect_mac)
    if not creds.host:
        return RungResult("fail", {**detail, "error": "add credentials-host.json (flax row)"})
    if not want:
        return RungResult("fail", {**detail, "error": "no discovered host MAC (discover first)"})
    user, pw = creds.host
    info: dict = {}
    state = bmc.power_status(bmc_ip, creds.bmc, info=info, runner=runner)
    detail["cipher"] = info.get("cipher")
    if state is None:
        return RungResult("fail", {**detail, "error": "power state unknown (no IPMI answer "
                                                      "on any credential/cipher); not acting"})
    ov = redfish.set_boot_pxe(bmc_ip, creds.redfish, ipmi_creds=creds.bmc, runner=runner)
    detail["override"] = ov.detail.get("method") if ov.verdict == "pass" else None
    if ov.verdict != "pass":
        return RungResult("fail", {**detail, "error": f"boot override: {ov.detail.get('error')}"})
    if state == "on":
        # the running OS's boot id, if it answers: a fresh boot must change it
        rc, out, _ = hostssh.run(host_ip, user, pw, _PROBE, timeout=20, runner=runner)
        if rc == 0:
            detail["old_boot_id"] = parse_probe(out)[0]
    action = "cycle" if state == "on" else "on"
    t0 = clock()                            # before the call: the action may land mid-call
    if not bmc.power_action(bmc_ip, creds.bmc, action, runner=runner):
        return RungResult("fail", {**detail, "power_action": action,
                                   "error": f"chassis power {action} failed"})
    detail["power_action"] = action
    end = t0 + deadline_s
    last = {"power": None, "ping": None, "ssh": None, "stale": None}
    while True:
        rc, out, err = hostssh.run(host_ip, user, pw, _PROBE, timeout=20, runner=runner)
        if rc == 0:
            bid, up, cmdline = parse_probe(out)
            since = clock() - t0
            stale = []
            if up is None:
                stale.append("uptime unreadable")
            elif up >= since:
                stale.append(f"uptime {int(up)}s >= {int(since)}s since power {action}")
            if detail["old_boot_id"] and bid == detail["old_boot_id"]:
                stale.append("boot_id unchanged")
            if not stale:
                detail.update(boot_id=bid, uptime_s=int(up), ssh_after_s=int(since))
                return _judge(detail, cmdline, host_ip, want)
            last["ssh"] = "rc=0 (old OS still answering)"
            last["stale"] = "; ".join(stale)
        else:
            last["ssh"] = f"rc={rc}" + (f" {err.strip()[-120:]}" if err.strip() else "")
        if clock() + poll_s >= end:
            break
        sleep(poll_s)
    last["power"] = bmc.power_status(bmc_ip, creds.bmc, runner=runner)
    last["ping"] = bmc.ping(host_ip, runner=runner)
    why = ("only the pre-reboot OS answered" if last["stale"] and last["ssh"].startswith("rc=0")
           else "no ssh to a fresh boot")
    return RungResult("fail", {**detail, "error": f"{why} within {deadline_s}s", "last": last})


def _judge(detail, cmdline, host_ip, want) -> RungResult:
    """A fresh boot: live image? the device Discover matched?"""
    detail["cmdline_ok"] = "BOOTIF=" in cmdline and "pxeaddr=" in cmdline
    if not detail["cmdline_ok"]:
        return RungResult("fail", {**detail, "error": "not the live image (no BOOTIF/pxeaddr)"})
    got = bootif_mac(cmdline)
    detail["bootif"] = got and _colon(got)
    if got != want:
        return RungResult("fail", {**detail, "error": f"wrong host at {host_ip}: BOOTIF "
                                   f"{detail['bootif'] or 'unparsed'} != discovered {_colon(want)}"})
    return RungResult("pass", detail)
