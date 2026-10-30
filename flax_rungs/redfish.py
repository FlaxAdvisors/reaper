"""Redfish boot override. The password rides an inherited pipe FD as a netrc, never argv."""
import json
import os
import subprocess

from . import bmc
from .result import RungResult

_TARGET = "/redfish/v1/Systems/system"


def _body(enable: bool, target: str = "Pxe") -> str:
    boot = ({"BootSourceOverrideEnabled": "Continuous", "BootSourceOverrideTarget": target,
             "BootSourceOverrideMode": "UEFI"} if enable else
            {"BootSourceOverrideEnabled": "Disabled", "BootSourceOverrideTarget": "None",
             "BootSourceOverrideMode": "UEFI"})
    return json.dumps({"Boot": boot})


def _patch(ip: str, user: str, pw: str, body: str, runner):
    r, w = os.pipe()
    try:
        os.write(w, f"machine {ip} login {user} password {pw}\n".encode())
        os.close(w)
        w = None
        argv = ["curl", "-sk", "--netrc-file", f"/dev/fd/{r}", "-X", "PATCH",
                "-H", "Content-Type: application/json", "-d", body,
                "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20",
                f"https://{ip}{_TARGET}"]
        return runner(argv, capture_output=True, text=True, timeout=30, pass_fds=(r,))
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        os.close(r)
        if w is not None:
            os.close(w)


def set_boot_pxe(bmc_ip: str, creds: list[tuple[str, str]], *, enable: bool = True,
                 ipmi_creds: list[tuple[str, str]] | None = None,
                 runner=subprocess.run) -> RungResult:
    body = _body(enable)
    last = ""
    for user, pw in creds:
        p = _patch(bmc_ip, user, pw, body, runner)
        code = ((p.stdout or "").strip() if p is not None else "")
        if code.startswith("2"):
            return RungResult("pass", {"method": "redfish", "user": user, "enable": enable})
        last = f"http {code}" if code else "no reply"
    info = {}
    if enable and bmc.bootdev_pxe(bmc_ip, ipmi_creds if ipmi_creds is not None else creds,
                                  info=info, runner=runner):
        return RungResult("pass", {"method": "ipmitool", "enable": True, "redfish": last,
                                   "cipher": info.get("cipher")})
    return RungResult("fail", {"error": last or "no Redfish credentials", "enable": enable})


def set_boot_disk(bmc_ip: str, creds: list[tuple[str, str]], *,
                  ipmi_creds: list[tuple[str, str]] | None = None,
                  runner=subprocess.run) -> RungResult:
    """Hdd/Continuous/UEFI (ghost setbootdisk's body): the node boots its installed OS
    whatever the post-install UEFI order says. ipmitool `bootdev disk` is the fallback."""
    body = _body(True, "Hdd")
    last = ""
    for user, pw in creds:
        p = _patch(bmc_ip, user, pw, body, runner)
        code = ((p.stdout or "").strip() if p is not None else "")
        if code.startswith("2"):
            return RungResult("pass", {"method": "redfish", "user": user, "target": "Hdd"})
        last = f"http {code}" if code else "no reply"
    info = {}
    if bmc.bootdev_disk(bmc_ip, ipmi_creds if ipmi_creds is not None else creds,
                        info=info, runner=runner):
        return RungResult("pass", {"method": "ipmitool", "target": "Hdd", "redfish": last,
                                   "cipher": info.get("cipher")})
    return RungResult("fail", {"error": last or "no Redfish credentials", "target": "Hdd"})
