"""Address plan for a rack order (spec section 6).

With cidr 172.<vid>.0.0/16 and OU = the device's BOTTOM OU:
  host   = 172.<vid>.<OU>.<c>          c: L->1, C->2, R->3, full-width->1
  BMC    = 172.<vid>.<OU>.<100+c>
  IOM n  = 172.<vid>.<OU>.<110+n>      n is 1-based (iom1, iom2, ...)
Hostnames: <prefix><pos-lower>, <prefix><pos-lower>-bmc, <prefix><pos-lower>-iom<n>.
Devices of kind "host+bmc" get host and bmc; kind "ioms" gets only its iomN links.
Collisions (same IP or same hostname) and IPs outside cidr raise AddressCollision.
"""
import ipaddress
import re
from dataclasses import dataclass

from .geometry import KIND_ROLES, Geometry, GeometryError

_COL = {"L": 1, "C": 2, "R": 3, None: 1}


class AddressCollision(GeometryError):
    pass


@dataclass(frozen=True)
class Addr:
    pos: str
    role: str       # host | bmc | iom1 | iom2 ...
    name: str
    ip: str


def plan_addresses(g: Geometry) -> list[Addr]:
    net = ipaddress.ip_network(g.cidr)
    out: list[Addr] = []
    for d in g.devices:
        base = f"{g.prefix}{d.pos.lower()}"
        c = _COL[d.col]
        ip = lambda last: f"172.{g.vid}.{d.ou}.{last}"
        if KIND_ROLES[d.kind] == "host+bmc":
            out.append(Addr(d.pos, "host", base, ip(c)))
            out.append(Addr(d.pos, "bmc", f"{base}-bmc", ip(100 + c)))
        else:
            for role in sorted(k for k in d.links if re.fullmatch(r"iom\d+", k)):
                n = int(role[3:])
                out.append(Addr(d.pos, role, f"{base}-{role}", ip(110 + n)))
    by_ip: dict[str, Addr] = {}
    by_name: dict[str, Addr] = {}
    for a in out:
        if ipaddress.ip_address(a.ip) not in net:
            raise AddressCollision(f"{a.pos} {a.role}: {a.ip} outside {g.cidr}")
        for seen, key, what in ((by_ip, a.ip, "ip"), (by_name, a.name, "hostname")):
            if key in seen:
                o = seen[key]
                raise AddressCollision(
                    f"{what} {key} computed for both {o.pos} ({o.role}) and {a.pos} ({a.role})")
            seen[key] = a
    return out
