"""Rack geometry model: parse and validate an order's geometry.json."""
import json
import re
from dataclasses import dataclass, field

KIND_ROLES = {"compute-tp": "host+bmc", "bc-head-tp": "host+bmc", "bc-jbod": "ioms"}

_LINK_RE = re.compile(r"^([A-Za-z]+):(\d+)(?:/(\d+))?$")


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
    col: str | None         # L / C / R, None = full width
    kind: str
    part: str
    links: dict[str, Link] = field(default_factory=dict)
    pop: str | None = None


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
    for x in data.get("devices", []):
        pos = _req(x, "pos", "device")
        kind = _req(x, "kind", pos)
        if kind not in KIND_ROLES:
            raise GeometryError(f"{pos}: unknown kind {kind!r}")
        if pos in seen:
            raise GeometryError(f"duplicate device pos {pos}")
        seen.add(pos)
        col = x.get("col")
        if col is not None and col not in ("L", "C", "R"):
            raise GeometryError(f"{pos}: bad col {col!r}")
        devices.append(Device(
            pos=pos, ou=int(_req(x, "ou", pos)), h=int(_req(x, "h", pos)), col=col,
            kind=kind, part=_req(x, "part", pos),
            links={k: parse_link(v) for k, v in x.get("links", {}).items()},
            pop=x.get("pop")))

    return Geometry(
        order=order, customer=_req(data, "customer", "geometry"),
        site=_req(data, "site", "geometry"), rack=rack, prefix=prefix,
        vid=int(_req(net, "vid", "network")), cidr=_req(net, "cidr", "network"),
        iperf_server=data.get("iperf_server"), switches=switches, devices=devices)


def load_geometry(path) -> Geometry:
    with open(path) as f:
        return parse_geometry(json.load(f))
