"""BMC reachability: ping, then an authenticated IPMI chassis power status."""
import os
import subprocess

from .result import RungResult


def ping(ip: str, *, runner=subprocess.run) -> bool:
    try:
        p = runner(["ping", "-c2", "-W1", ip], capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0


def reach(ip: str, creds: list[tuple[str, str]], *, cipher: int = 17,
          tries: int = 3, runner=subprocess.run) -> RungResult:
    up = ping(ip, runner=runner)
    detail = {"ping": up, "power": None, "user": None}
    if not up:
        detail["error"] = "no ping reply"
        return RungResult("fail", detail)
    last = ""
    for user, pw in creds:
        env = dict(os.environ)
        env["IPMI_PASSWORD"] = pw          # -E reads it; never -P (argv)
        argv = ["ipmitool", "-I", "lanplus", "-C", str(cipher), "-H", ip,
                "-U", user, "-E", "chassis", "power", "status"]
        for _ in range(tries):             # RAKP is flaky on some BMCs
            try:
                p = runner(argv, capture_output=True, text=True, timeout=20, env=env)
            except (subprocess.TimeoutExpired, OSError) as e:
                last = str(e)
                continue
            if "Chassis Power" in (p.stdout or ""):
                detail["power"] = "on" if "is on" in p.stdout else "off"
                detail["user"] = user
                return RungResult("pass", detail)
            last = (p.stderr or p.stdout or f"rc={p.returncode}").strip()
    detail["error"] = last or "no BMC credentials"
    return RungResult("fail", detail)


def _ipmi(ip: str, user: str, pw: str, args: list[str], *, cipher: int = 17,
          runner=subprocess.run):
    env = dict(os.environ)
    env["IPMI_PASSWORD"] = pw              # -E reads it; never -P (argv)
    argv = ["ipmitool", "-I", "lanplus", "-C", str(cipher), "-H", ip,
            "-U", user, "-E"] + args
    try:
        return runner(argv, capture_output=True, text=True, timeout=30, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return None


def power_status(ip: str, creds: list[tuple[str, str]], *, runner=subprocess.run):
    """'on' / 'off', or None when no credential got an answer."""
    for user, pw in creds:
        p = _ipmi(ip, user, pw, ["chassis", "power", "status"], runner=runner)
        if p is not None and "Chassis Power" in (p.stdout or ""):
            return "on" if "is on" in p.stdout else "off"
    return None


def power_action(ip: str, creds: list[tuple[str, str]], action: str, *,
                 runner=subprocess.run) -> bool:
    """action: 'on' | 'cycle' | 'off'; True when a credential's ipmitool exited 0."""
    for user, pw in creds:
        p = _ipmi(ip, user, pw, ["chassis", "power", action], runner=runner)
        if p is not None and p.returncode == 0:
            return True
    return False


def bootdev_pxe(ip: str, creds: list[tuple[str, str]], *, runner=subprocess.run) -> bool:
    for user, pw in creds:
        p = _ipmi(ip, user, pw,
                  ["chassis", "bootdev", "pxe", "options=efiboot,persistent"],
                  runner=runner)
        if p is not None and p.returncode == 0:
            return True
    return False


def parse_mc_info(text: str) -> dict:
    """`ipmitool mc info` -> {"firmware": "Firmware Revision", "aux": "Aux Firmware Rev Info"
    (its indented continuation lines joined by spaces)}; None for what is absent."""
    fw, aux, in_aux = None, [], False
    for line in text.splitlines():
        if line[:1] not in (" ", "\t") and ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            in_aux = k == "Aux Firmware Rev Info"
            if k == "Firmware Revision":
                fw = v or None
            elif in_aux and v:
                aux.append(v)
        elif in_aux and line.strip():
            aux.append(line.strip())
    return {"firmware": fw, "aux": " ".join(aux) or None}


def mc_info(ip: str, creds: list[tuple[str, str]], *, runner=subprocess.run) -> dict | None:
    """Out-of-band `mc info` with the first credential that answers; None if none did."""
    for user, pw in creds:
        p = _ipmi(ip, user, pw, ["mc", "info"], runner=runner)
        if p is not None and p.returncode == 0 and "Firmware Revision" in (p.stdout or ""):
            return parse_mc_info(p.stdout)
    return None
