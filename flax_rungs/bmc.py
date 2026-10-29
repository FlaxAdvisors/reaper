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
