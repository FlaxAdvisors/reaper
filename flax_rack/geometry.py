"""Rack geometry model: parse and validate an order's geometry.json."""
import json
import re
from dataclasses import dataclass, field

# what each kind exposes on the rack network (addressing.py turns these into IPs).
# A geometry device may override with its own "endpoints" (e.g. a Yosemite sled
# with 4 cards: {"bmc": 1, "hosts": 4}).
KIND_ENDPOINTS = {"compute-tp": {"bmc": 1, "hosts": 1},
                  "bc-head-tp": {"bmc": 1, "hosts": 1},
                  "bc-jbod": {"ioms": 2}}
_EP_KEYS = ("hosts", "bmc", "ioms")
MAX_COL = 5
COL_ALIAS = {"L": 1, "C": 2, "R": 3}

_LINK_RE = re.compile(r"^([A-Za-z]+):(\d+)(?:/(\d+))?$")
# a pos becomes a directory name (artifacts/<pos>/ under a root daemon) and part of
# Kea hostnames: no dots, slashes or spaces
_POS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


class GeometryError(Exception):
    pass


@dataclass(frozen=True)
class Link:
    switch: str
    port: int
    lane: int | None


def parse_link(s: str) -> Link:
    m = _LINK_RE.match(s) if isinstance(s, str) else None
    if not m:
        raise GeometryError(f"bad link {s!r} (want 'C:7' or 'A:28/1')")
    return Link(m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None)


@dataclass
class Switch:
    id: str
    role: str
    ou: int
    model: str
    nos: str
    mgmt_ip: str | None = None
    uplink_ports: list[int] = field(default_factory=list)


@dataclass
class Device:
    pos: str
    ou: int                 # bottom OU
    h: int
    col: int | None         # 1..5 (L/C/R = 1/2/3), None = full width (addressed as 1)
    kind: str
    part: str
    links: dict[str, Link] = field(default_factory=dict)
    pop: str | None = None
    endpoints: dict[str, int] = field(default_factory=dict)   # {hosts, bmc, ioms}, all keys


@dataclass
class Geometry:
    order: str
    customer: str
    site: str
    rack: str
    prefix: str
    vid: int
    cidr: str
    iperf_server: str | None
    switches: list[Switch]
    devices: list[Device]

    def device(self, pos: str) -> Device:
        for d in self.devices:
            if d.pos == pos:
                return d
        raise KeyError(pos)

    @property
    def url_prefix(self) -> str:
        return self.prefix.rstrip("-")

    @property
    def kea_source(self) -> str:
        return f"order:{self.order}" + (f"/{self.rack}" if self.rack else "")


def parse_col(col, ctx: str = "device") -> int | None:
    """Column alias L/C/R or an int 1..5 -> int; None (full width) stays None."""
    if col is None:
        return None
    if isinstance(col, str) and col in COL_ALIAS:
        return COL_ALIAS[col]
    if isinstance(col, int) and not isinstance(col, bool) and 1 <= col <= MAX_COL:
        return col
    raise GeometryError(f"{ctx}: bad col {col!r} (want L/C/R or 1..{MAX_COL})")


def parse_endpoints(kind: str, ep, ctx: str) -> dict[str, int]:
    """The kind's endpoints, or the device's own override, with every key present."""
    if ep is None:
        ep = KIND_ENDPOINTS[kind]
    if not isinstance(ep, dict) or set(ep) - set(_EP_KEYS):
        raise GeometryError(f"{ctx}: bad endpoints {ep!r} (keys {', '.join(_EP_KEYS)})")
    out = {k: ep.get(k, 0) for k in _EP_KEYS}
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in out.values()):
        raise GeometryError(f"{ctx}: endpoints must be non-negative ints, got {ep!r}")
    if out["hosts"] > 9:
        raise GeometryError(f"{ctx}: {out['hosts']} hosts (max 9 cards per device)")
    if out["bmc"] > 1:
        raise GeometryError(f"{ctx}: bmc must be 0 or 1, got {out['bmc']}")
    if out["bmc"] + out["ioms"] > 9:
        raise GeometryError(f"{ctx}: bmc + ioms = {out['bmc'] + out['ioms']} (max 9)")
    if not any(out.values()):
        raise GeometryError(f"{ctx}: no endpoints")
    return out


def _req(d: dict, key: str, ctx: str):
    if key not in d:
        raise GeometryError(f"{ctx}: missing {key!r}")
    return d[key]


def parse_geometry(data: dict) -> Geometry:
    order = _req(data, "order", "geometry")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]*", str(order)):
        raise GeometryError(f"bad order id {order!r}")
    rack = data.get("rack", "")
    if rack != "" and not re.fullmatch(r"[a-z]", str(rack)):
        raise GeometryError(f"rack must be '' or one lower-case letter, got {rack!r}")
    net = _req(data, "network", "geometry")
    prefix = _req(net, "prefix", "network")
    if rack and not prefix.endswith(rack + "-"):
        raise GeometryError(f"prefix {prefix!r} must end with {rack + '-'!r} for rack {rack!r}")

    switches = []
    for s in data.get("switches", []):
        switches.append(Switch(
            id=_req(s, "id", "switch"), role=_req(s, "role", "switch"),
            ou=int(_req(s, "ou", "switch")), model=_req(s, "model", "switch"),
            nos=_req(s, "nos", "switch"), mgmt_ip=s.get("mgmt_ip"),
            uplink_ports=[int(p) for p in s.get("uplink_ports", [])]))

    devices = []
    seen = set()
    mgmt = {s.id for s in switches if s.role == "mgmt"}
    ports: dict[Link, str] = {}       # mgmt (switch, port, lane) -> "pos link"
    for x in data.get("devices", []):
        pos = _req(x, "pos", "device")
        kind = _req(x, "kind", pos)
        if not isinstance(pos, str) or not _POS_RE.fullmatch(pos):
            raise GeometryError(f"bad device pos {pos!r} (want [A-Za-z0-9][A-Za-z0-9_-]{{0,31}})")
        if kind not in KIND_ENDPOINTS:
            raise GeometryError(f"{pos}: unknown kind {kind!r}")
        if pos in seen:
            raise GeometryError(f"duplicate device pos {pos}")
        seen.add(pos)
        col = parse_col(x.get("col"), pos)
        endpoints = parse_endpoints(kind, x.get("endpoints"), pos)
        links = {k: parse_link(v) for k, v in x.get("links", {}).items()}
        for name, l in links.items():
            # two devices on one mgmt-switch port would match the same MACs and the
            # second reserve would silently move the first one's reservation.
            # ToR links are not matched in Plan 1, and the BOM seed repeats them
            # (RRORD-101: L37/C37/R37 all pcie0 A:8/4), so they are not checked here.
            if l.switch not in mgmt:
                continue
            if l in ports:
                raise GeometryError(f"{ports[l]} and {pos} {name} share switch port "
                                    f"{l.switch}:{l.port}" + (f"/{l.lane}" if l.lane else ""))
            ports[l] = f"{pos} {name}"
        devices.append(Device(
            pos=pos, ou=int(_req(x, "ou", pos)), h=int(_req(x, "h", pos)), col=col,
            kind=kind, part=_req(x, "part", pos), links=links, pop=x.get("pop"),
            endpoints=endpoints))

    return Geometry(
        order=order, customer=_req(data, "customer", "geometry"),
        site=_req(data, "site", "geometry"), rack=rack, prefix=prefix,
        vid=int(_req(net, "vid", "network")), cidr=_req(net, "cidr", "network"),
        iperf_server=data.get("iperf_server"), switches=switches, devices=devices)


def load_geometry(path) -> Geometry:
    with open(path) as f:
        return parse_geometry(json.load(f))
