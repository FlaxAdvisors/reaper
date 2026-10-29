"""Match mgmt-switch FDB entries to geometry slots.

Conservative by design: a port whose MACs do not fit its slot is reported
ambiguous/unexpected, never guessed into a reservation. The one derivation: a
host+bmc port with a single MAC is taken as the BMC of a powered-off node and
its host MAC is BMC-2 (BMC = host+2 is a hard rule on this platform).

Matching is per kind via MATCHERS. A kind with no matcher, or a device whose
endpoints override its kind's shape (e.g. a 4-card sled), is reported
'unsupported' ("no MAC matcher for kind <k>"), never guessed.
"""
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from flax_rack.geometry import KIND_ENDPOINTS, Device, Geometry, parse_endpoints
from flax_rungs.sonic import FdbEntry, iface_port


@dataclass
class DeviceMatch:
    pos: str
    status: str                       # found / missing / ambiguous / unsupported
    host_mac: str | None = None
    bmc_mac: str | None = None
    iom_macs: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Match:
    devices: dict[str, DeviceMatch]
    unexpected: list[tuple[int, str]]   # (port, mac)


def _port_macs(fdb: list[FdbEntry]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for e in fdb:
        try:
            port = iface_port(e.iface)
        except ValueError:
            continue                   # PortChannel, Vlan, etc.
        macs = out.setdefault(port, [])
        if e.mac not in macs:
            macs.append(e.mac)
    for macs in out.values():
        macs.sort(key=lambda m: int(m, 16))
    return out


def _match_host_bmc_macs(pos: str, macs: list[str]) -> DeviceMatch:
    if not macs:
        return DeviceMatch(pos, "missing", note="no MAC on port")
    if len(macs) == 1 and int(macs[0], 16) >= 2:
        # powered-off node (or host MAC aged out): only the BMC talks. BMC = host+2
        # is a hard rule on this platform, so the host is derived (controller ruling)
        return DeviceMatch(pos, "found", host_mac=f"{int(macs[0], 16) - 2:012x}",
                           bmc_mac=macs[0], note="host derived (host MAC not seen)")
    if len(macs) == 2 and int(macs[1], 16) == int(macs[0], 16) + 2:
        return DeviceMatch(pos, "found", host_mac=macs[0], bmc_mac=macs[1])
    return DeviceMatch(pos, "ambiguous",
                       note="want host + bmc(host+2), saw " + ",".join(macs))


def _match_ioms(pos: str, iom_ports: list[tuple[int, int]],
                by_port: dict[int, list[str]]) -> DeviceMatch:
    n = max((i for i, _ in iom_ports), default=0)
    ioms = [""] * n
    missing, bad = [], []
    for i, port in iom_ports:
        macs = by_port.get(port, [])
        if not macs:
            missing.append(f"iom{i}")
        elif len(macs) != 1:
            bad.append(f"iom{i}: " + ",".join(macs))
        else:
            ioms[i - 1] = macs[0]
    if bad:
        return DeviceMatch(pos, "ambiguous", note="want one MAC per IOM port, saw " + "; ".join(bad))
    if missing:
        return DeviceMatch(pos, "missing", note="no MAC on " + ",".join(missing))
    return DeviceMatch(pos, "found", iom_macs=ioms)


def match_host_bmc(d: Device, mine: dict[str, int], by_port, switch_id: str) -> DeviceMatch:
    """TiogaPass: host + BMC (host+2) share the mezz0 port."""
    if "mezz0" not in mine:
        return DeviceMatch(d.pos, "missing", note=f"no {switch_id} mezz0 link")
    return _match_host_bmc_macs(d.pos, by_port.get(mine["mezz0"], []))


def match_ioms(d: Device, mine: dict[str, int], by_port, switch_id: str) -> DeviceMatch:
    """One MAC per iomN port (Bryce Canyon JBOD)."""
    iom_ports = sorted((int(m.group(1)), p) for name, p in mine.items()
                       if (m := re.fullmatch(r"iom(\d+)", name)))
    return _match_ioms(d.pos, iom_ports, by_port)


Matcher = Callable[[Device, dict[str, int], dict[int, list[str]], str], DeviceMatch]
MATCHERS: dict[str, Matcher] = {
    "compute-tp": match_host_bmc,
    "bc-head-tp": match_host_bmc,
    "bc-jbod": match_ioms,
}


def matcher_for(d: Device) -> Matcher | None:
    # a matcher is written for its kind's endpoint shape; an override is a different device
    if d.kind not in KIND_ENDPOINTS or d.endpoints != parse_endpoints(d.kind, None, d.pos):
        return None
    return MATCHERS.get(d.kind)


def match(g: Geometry, fdb: list[FdbEntry], switch_id: str = "C") -> Match:
    uplinks = {p for s in g.switches if s.id == switch_id for p in s.uplink_ports}
    by_port = _port_macs(fdb)
    assigned: set[int] = set()
    devices: dict[str, DeviceMatch] = {}
    for d in g.devices:
        mine = {name: l.port for name, l in d.links.items() if l.switch == switch_id}
        assigned.update(mine.values())
        fn = matcher_for(d)
        if fn is None:
            ep = ", ".join(f"{k} {v}" for k, v in d.endpoints.items() if v)
            devices[d.pos] = DeviceMatch(d.pos, "unsupported",
                                         note=f"no MAC matcher for kind {d.kind} ({ep})")
        else:
            devices[d.pos] = fn(d, mine, by_port, switch_id)
    unexpected = [(p, m) for p in sorted(by_port) if p not in assigned and p not in uplinks
                  for m in by_port[p]]
    return Match(devices, unexpected)
