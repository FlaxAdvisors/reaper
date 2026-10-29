"""Static fabric addressing on the live image: the NIC port LLDP shows on wedge A gets
10.201.<OU>.<x>/16, on wedge B 10.202.<OU>.<x>/16 (x = the host's last octet).
Applied with `ip addr replace` (idempotent, not persistent; re-applied each Qualify)."""
import shlex
import subprocess

from . import hostssh
from .result import RungResult

_NET = {"A": 201, "B": 202}


def fabric_addr(g, d, side: str, host_octet: int) -> str:
    """CIDR for device d's fabric port on wedge `side` ("A"/"B"). `host_octet` is the last
    octet of the host's rack address (the caller's flax_rack.addressing plan, never
    re-derived here). ValueError if d has no host."""
    if side not in _NET:
        raise ValueError(f"bad side {side!r} (want 'A' or 'B')")
    if not d.endpoints.get("hosts"):
        raise ValueError(f"{d.pos} has no host; no fabric address")
    if not isinstance(host_octet, int) or not 1 <= host_octet <= 254:
        raise ValueError(f"{d.pos}: bad host octet {host_octet!r}")
    return f"10.{_NET[side]}.{d.ou}.{host_octet}/16"


def assignments(g, d, links: list[dict], host_octet: int) -> dict[str, str]:
    """ifname -> cidr from HA-path detail["links"]; the side comes from `seen`
    (the wedge LLDP actually showed), not from the geometry's expectation."""
    out = {}
    for l in links:
        if not l.get("seen") or not l.get("ifname"):
            continue
        out[l["ifname"]] = fabric_addr(g, d, l["seen"].split()[0], host_octet)
    return out


def apply(host_ip: str, user: str, pw: str, assigns: dict[str, str], *,
          runner=subprocess.run) -> RungResult:
    if not assigns:
        return RungResult("fail", {"error": "no fabric ports to address"})
    lines = []
    for ifn, cidr in assigns.items():
        q, c = shlex.quote(ifn), shlex.quote(cidr)
        lines.append(f"ip link set {q} up && ip addr replace {c} dev {q} "
                     f"&& ip -4 -o addr show dev {q} | grep -qF {shlex.quote(' ' + cidr.split('/')[0] + '/')} "
                     f"&& echo OK {q} || echo FAIL {q}")
    rc, out = hostssh.run(host_ip, user, pw, "\n".join(lines) + "\n", runner=runner)
    ok = {ln.split()[1] for ln in out.splitlines() if ln.startswith("OK ")}
    bad = [i for i in assigns if i not in ok]
    detail = dict(assigns)
    if rc != 0 or bad:
        detail["error"] = (f"fabric addressing failed on {', '.join(bad) or 'all ports'}"
                           + (f" (rc={rc})" if rc else ""))
        return RungResult("fail", detail)
    return RungResult("pass", detail)
