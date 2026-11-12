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
from flax_rack.switches import FdbEntry, front_panel, link_iface

PortKey = tuple[int, int | None]       # (front-panel port, breakout lane or None)


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


def port_label(sw, key: PortKey) -> str:
    """The port as an operator reads it on the faceplate / in EOS tables: Et5/1."""
    port, lane = key
    if getattr(sw, "nos", None) == "eos":
        return f"Et{port}" + (f"/{lane}" if lane else "")
    return link_iface(sw, port, lane) if sw else f"{port}" + (f"/{lane}" if lane else "")


def key_of(link) -> PortKey:
    return (link.port, link.lane)


def _port_macs(fdb: list[FdbEntry], sw=None) -> dict[PortKey, list[str]]:
    """(front-panel port, lane) -> its MACs (sw: the switch the FDB is from; its nos/model
    name the ports; None = a Dell SONiC mgmt switch, EthernetN = port N+1). Breakout
    lanes stay apart: two devices on Et5/1 and Et5/4 are two keys."""
    out: dict[PortKey, list[str]] = {}
    for e in fdb:
        fp = front_panel(sw, e.iface)
        if fp is None:
            continue                   # PortChannel, Vlan, etc.
        macs = out.setdefault(fp, [])
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


def _match_ioms(pos: str, iom_ports: list[tuple[int, PortKey]],
                by_port: dict[PortKey, list[str]]) -> DeviceMatch:
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


def match_host_bmc(d: Device, mine: dict[str, PortKey], by_port, switch_id: str) -> DeviceMatch:
    """TiogaPass: host + BMC (host+2) share the mezz0 port."""
    if "mezz0" not in mine:
        return DeviceMatch(d.pos, "missing", note=f"no {switch_id} mezz0 link")
    return _match_host_bmc_macs(d.pos, by_port.get(mine["mezz0"], []))


def match_ioms(d: Device, mine: dict[str, PortKey], by_port, switch_id: str) -> DeviceMatch:
    """One MAC per iomN port (Bryce Canyon JBOD)."""
    iom_ports = sorted((int(m.group(1)), p) for name, p in mine.items()
                       if (m := re.fullmatch(r"iom(\d+)", name)))
    return _match_ioms(d.pos, iom_ports, by_port)


def match_none(d: Device, mine: dict[str, PortKey], by_port, switch_id: str) -> DeviceMatch:
    """A kind with no switch-side identity yet (an enclosure's own BMC that is not cabled to
    the rack switch -- judged through its head -- or a Yosemite chassis: a later plan)."""
    return DeviceMatch(d.pos, "unsupported", note=f"no MAC matcher for kind {d.kind}")


def match_placed(d: Device, mine: dict[str, PortKey], by_port, switch_id: str,
                 sw=None) -> DeviceMatch:
    """A lab rack's placed device: the MACs recorded at placement (paired by the BMC's FRU
    answer, not by an offset) on the placed port = found; seen on ANOTHER port = moved
    (flagged, never re-placed); else missing."""
    p = d.placed or {}
    want = [m for m in (p.get("host_mac"), p.get("bmc_mac")) if m]
    key = mine.get("mezz0")
    here = by_port.get(key, []) if key else []
    if any(m in here for m in want):
        # either placed MAC on the placed port is this hardware (a powered-off host, or a
        # BMC whose MAC aged out of the FDB, leaves only the other one)
        host, bmc = p.get("host_mac"), p.get("bmc_mac")
        unseen = [n for n, m in (("host", host), ("BMC", bmc)) if m and m not in here]
        return DeviceMatch(d.pos, "found", host_mac=host, bmc_mac=bmc,
                           note=", ".join(f"{n} MAC not seen" for n in unseen))
    for k, macs in by_port.items():
        if k != key and any(m in macs for m in want):
            where = port_label(sw, k)
            return DeviceMatch(d.pos, "moved",
                               note=f"serial {p.get('serial') or '?'} now on port {where}")
    if here:
        # something else answers on the placed port: a swapped blade, never re-placed here
        return DeviceMatch(d.pos, "replaced",
                           note=f"placed {p.get('serial') or '?'} not on its port; other MACs "
                                f"there: {', '.join(here)} (unplace, then place what is there)")
    return DeviceMatch(d.pos, "missing", note="placed MACs not on the port")


Matcher = Callable[[Device, dict[str, PortKey], dict[PortKey, list[str]], str], DeviceMatch]
MATCHERS: dict[str, Matcher] = {
    "compute-tp": match_host_bmc,
    "bc-head-tp": match_host_bmc,
    "bc-jbod": match_ioms,
    "compute-leopard": match_host_bmc,
    "bb-head": match_host_bmc,
    "lightning-head": match_host_bmc,
    "bc-head": match_host_bmc,
    "gc-head": match_host_bmc,
    "grandcanyon": match_ioms,
    "bigbasin": match_none,
    "lightning": match_none,
    "yv2": match_none,
    "yv3": match_none,
}


def matcher_for(d: Device) -> Matcher | None:
    # a matcher is written for its kind's endpoint shape; an override is a different device
    if d.kind not in KIND_ENDPOINTS or d.endpoints != parse_endpoints(d.kind, None, d.pos):
        return None
    return MATCHERS.get(d.kind)


def match(g: Geometry, fdb: list[FdbEntry], switch_id: str = "C") -> Match:
    sw = next((s for s in g.switches if s.id == switch_id), None)
    uplinks = set(sw.uplink_ports) if sw else set()
    by_port = _port_macs(fdb, sw)
    assigned: set[PortKey] = set()
    devices: dict[str, DeviceMatch] = {}
    for d in g.devices:
        mine = {name: key_of(l) for name, l in d.links.items() if l.switch == switch_id}
        assigned.update(mine.values())
        fn = matcher_for(d)
        if d.placed is not None:
            devices[d.pos] = match_placed(d, mine, by_port, switch_id, sw)
        elif fn is None:
            ep = ", ".join(f"{k} {v}" for k, v in d.endpoints.items() if v)
            devices[d.pos] = DeviceMatch(d.pos, "unsupported",
                                         note=f"no MAC matcher for kind {d.kind} ({ep})")
        else:
            devices[d.pos] = fn(d, mine, by_port, switch_id)
    # the front-panel port only (an int, as the port tables sort it); a lab rack's lanes
    # are listed in full by Unplaced
    unexpected = [(k[0], m)
                  for k in sorted(by_port, key=lambda k: (k[0], k[1] or 0))
                  if k not in assigned and k[0] not in uplinks for m in by_port[k]]
    return Match(devices, unexpected)
