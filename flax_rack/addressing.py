"""Address plan for a rack order (spec section 6, operator-approved 50-per-column blocks).

With cidr 172.<vid>.0.0/16, OU = the device's BOTTOM OU, col 1..5 (L/C/R = 1/2/3,
full width = 1), every device owns the block 50*(col-1)+1 .. 50*(col-1)+50 of
172.<vid>.<OU>.x, and within it slot k:
  k  1..9    host / card k              (hosts endpoints)
  k 11..19   BMC, then IOM n             (bmc first at 11, IOM n at 10+bmc+n)
  k 21..29   spare (unassigned)
e.g. L37 host .37.1 BMC .37.11; C37 .37.51/.37.61; R05 .5.101/.5.111;
JBOD01 IOM1/2 .1.11/.1.12; a 4-card sled at OU20 col 4 .20.151-154, BMC .20.161.
Roles: `host` (single-host devices, Plan 1's name), host1..host9 (multi-host),
bmc, iom1..iom9. Hostnames: <prefix><pos>, <prefix><pos>-n<k> (multi-host),
<prefix><pos>-bmc, <prefix><pos>-iom<n>.
Collisions (same IP or same hostname) and IPs outside cidr raise AddressCollision.
"""
import ipaddress
from dataclasses import dataclass

from .geometry import Device, Geometry, GeometryError

BLOCK = 50


class AddressCollision(GeometryError):
    pass


@dataclass(frozen=True)
class Addr:
    pos: str
    role: str       # host | host1..host9 | bmc | iom1..iom9
    name: str
    ip: str


def device_addrs(vid: int, prefix: str, d: Device) -> list[Addr]:
    base = f"{prefix}{d.pos.lower()}"
    off = BLOCK * ((d.col or 1) - 1)
    ip = lambda k: f"172.{vid}.{d.ou}.{off + k}"
    ep = d.endpoints
    out: list[Addr] = []
    if ep["hosts"] == 1:
        out.append(Addr(d.pos, "host", base, ip(1)))
    else:
        for k in range(1, ep["hosts"] + 1):
            out.append(Addr(d.pos, f"host{k}", f"{base}-n{k}", ip(k)))
    if ep["bmc"]:
        out.append(Addr(d.pos, "bmc", f"{base}-bmc", ip(11)))
    for n in range(1, ep["ioms"] + 1):
        out.append(Addr(d.pos, f"iom{n}", f"{base}-iom{n}", ip(10 + ep["bmc"] + n)))
    return out


def plan_addresses(g: Geometry) -> list[Addr]:
    net = ipaddress.ip_network(g.cidr)
    out: list[Addr] = []
    for d in g.devices:
        out += device_addrs(g.vid, g.prefix, d)
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
