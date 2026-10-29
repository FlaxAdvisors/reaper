"""boot_live: force a PXE boot, power the node, wait for ssh, prove it is the live image."""
import subprocess
import time

from . import bmc, hostssh, redfish
from .result import RungResult


def boot_live(bmc_ip: str, host_ip: str, creds, *, deadline_s: int = 900,
              runner=subprocess.run, sleep=time.sleep, clock=time.monotonic) -> RungResult:
    detail = {"override": None, "power_action": None, "ssh_after_s": None,
              "cmdline_ok": False}
    if not creds.host:
        return RungResult("fail", {**detail, "error": "add credentials-host.json (flax row)"})
    ov = redfish.set_boot_pxe(bmc_ip, creds.redfish, ipmi_creds=creds.bmc, runner=runner)
    detail["override"] = ov.detail.get("method") if ov.verdict == "pass" else None
    if ov.verdict != "pass":
        return RungResult("fail", {**detail, "error": f"boot override: {ov.detail.get('error')}"})
    state = bmc.power_status(bmc_ip, creds.bmc, runner=runner)
    action = "cycle" if state == "on" else "on"
    if not bmc.power_action(bmc_ip, creds.bmc, action, runner=runner):
        return RungResult("fail", {**detail, "power_action": action,
                                   "error": f"chassis power {action} failed"})
    detail["power_action"] = action
    t0 = clock()
    user, pw = creds.host
    last = {"power": None, "ping": None, "ssh": None}
    if not hostssh.wait_ssh(host_ip, user, pw, deadline_s=deadline_s, last=last,
                            runner=runner, sleep=sleep, clock=clock):
        last["power"] = bmc.power_status(bmc_ip, creds.bmc, runner=runner)
        last["ping"] = bmc.ping(host_ip, runner=runner)
        return RungResult("fail", {**detail, "error": f"no ssh within {deadline_s}s",
                                   "last": last})
    detail["ssh_after_s"] = int(clock() - t0)
    rc, out = hostssh.run(host_ip, user, pw, "cat /proc/cmdline", runner=runner)
    detail["cmdline_ok"] = rc == 0 and "BOOTIF=" in out and "pxeaddr=" in out
    if not detail["cmdline_ok"]:
        return RungResult("fail", {**detail, "error": "not the live image (no BOOTIF/pxeaddr)"})
    return RungResult("pass", detail)
