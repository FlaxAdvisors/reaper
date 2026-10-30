"""Site Cisco IOS switch (the turtle) reads: which MACs sit on a rack switch's mgmt ports.

Transport mirrors flax_switch_sense/driver_ios.py (classic-IOS kex/hostkey options,
-tt, `terminal length 0` then the show commands on stdin, never ControlMaster), except
that the password rides SSHPASS (sshpass -e), never argv. Parsing mirrors its
mac_address_table: `vlan dotted-mac DYNAMIC port` rows only.

The turtle ages MACs out after 300 s, so a quiet device looks absent: the rung pings
the device IPs it knows first, and re-reads once (after another ping) before it
reports a port empty."""
import os
import re
import subprocess

from .bmc import ping
from .result import RungResult

_PORT_ARG = re.compile(r"^(?:Gi|Te|Fa|Tw|Hu|Fo|Twe)\d+(?:/\d+){0,3}$")
_LONG = (("TwentyFiveGigE", "Twe"), ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
         ("FastEthernet", "Fa"), ("HundredGigE", "Hu"), ("FortyGigabitEthernet", "Fo"))


class IosError(RuntimeError):
    pass


def short_port(port: str) -> str:
    """Gi1/0/18 from GigabitEthernet1/0/18 (IOS prints the short form in the table)."""
    for long, short in _LONG:
        if port.startswith(long):
            return short + port[len(long):]
    return port


def mac12(dotted: str) -> str:
    """0090fb64b65d from 0090.fb64.b65d; '' when not a MAC."""
    digits = "".join(c for c in dotted.lower() if c in "0123456789abcdef")
    return digits if len(digits) == 12 else ""


def parse_mac_table(text: str) -> list[tuple[int, str, str]]:
    """[(vlan, mac12, port)] for every DYNAMIC unicast row (STATIC CPU/VRRP rows are
    infrastructure, not a device). Prompt echoes and headers never match."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or "." not in parts[1] or not parts[0].isdigit():
            continue
        if parts[2].upper() != "DYNAMIC":
            continue
        mac = mac12(parts[1])
        if mac:
            out.append((int(parts[0]), mac, parts[3]))
    return out


def _argv(host: str, user: str) -> list[str]:
    return ["sshpass", "-e", "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "KexAlgorithms=+diffie-hellman-group14-sha1",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8",
            f"{user}@{host}"]


def show(host: str, user: str, pw: str, cmds: list[str], *, runner=subprocess.run,
         timeout: int = 30) -> str:
    """Combined stdout of `cmds` (show commands only) in one ssh session."""
    for c in cmds:
        if not c.startswith("show "):
            raise IosError(f"not a show command: {c!r}")
    env = dict(os.environ)
    env["SSHPASS"] = pw
    payload = "terminal length 0\n" + "\n".join(cmds) + "\n"
    try:
        p = runner(_argv(host, user), input=payload, capture_output=True, text=True,
                   timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise IosError(f"{host}: timed out after {timeout}s") from e
    except OSError as e:
        raise IosError(f"{host}: ssh exec failed: {e}") from e
    if p.returncode != 0:
        raise IosError(f"{host}: ssh rc={p.returncode}: "
                       + ((p.stderr or "").strip()[-300:] or "no output"))
    return p.stdout or ""


def _read(host, user, pw, ports, runner):
    text = show(host, user, pw, [f"show mac address-table interface {p}" for p in ports],
                runner=runner)
    got = {p: [] for p in ports}
    want = {short_port(p): p for p in ports}
    for _vlan, mac, port in parse_mac_table(text):
        p = want.get(short_port(port))
        if p is not None and mac not in got[p]:
            got[p].append(mac)
    return got, text


def mac_table(host: str, user: str, pw: str, ports: list[str], *, ping_ips=(),
              runner=subprocess.run) -> RungResult:
    """detail {"ports": {port: [mac12, ...]}, "pinged": {ip: bool}, "reread": bool} +
    headline/rows/notes. pass when every port shows a MAC, fail when one stays empty
    after the re-read or the switch could not be read."""
    bad = [p for p in ports if not _PORT_ARG.match(short_port(p))]
    if not ports or bad:
        return RungResult("fail", {"error": f"bad port name(s): {bad or 'none given'}",
                                   "headline": "no turtle read", "rows": [], "notes": []})
    pinged = {ip: ping(ip, runner=runner) for ip in ping_ips}
    arts, reread = {}, False
    try:
        got, arts["show-mac.txt"] = _read(host, user, pw, ports, runner)
        if any(not v for v in got.values()):
            reread = True
            pinged.update({ip: ping(ip, runner=runner) or pinged.get(ip, False)
                           for ip in ping_ips})
            got, arts["show-mac-reread.txt"] = _read(host, user, pw, ports, runner)
    except IosError as e:
        return RungResult("fail", {"error": str(e), "pinged": pinged,
                                   "headline": f"turtle {host} not read", "rows": [],
                                   "notes": [str(e)]}, arts)
    empty = [p for p, v in got.items() if not v]
    rows = [[p, ", ".join(v) or "no MAC"] for p, v in got.items()]
    rows += [[f"ping {ip}", "reply" if ok else "no reply"] for ip, ok in pinged.items()]
    notes = ["the turtle ages a MAC out after 300 s: device IPs are pinged before the read"]
    if reread:
        notes.append("a port was empty on the first read; re-read once after another ping")
    detail = {"ports": got, "pinged": pinged, "reread": reread, "rows": rows, "notes": notes}
    if empty:
        detail["error"] = f"no MAC on {', '.join(empty)}"
        detail["headline"] = f"no MAC on {', '.join(empty)} at {host}"
        return RungResult("fail", detail, arts)
    detail["headline"] = f"{sum(map(len, got.values()))} MAC(s) on {len(got)} port(s)"
    return RungResult("pass", detail, arts)
