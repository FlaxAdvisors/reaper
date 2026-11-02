"""Rack geometry model: parse and validate an order's geometry.json."""
import json
import re
from dataclasses import dataclass, field, replace

# what each kind exposes on the rack network (addressing.py turns these into IPs).
# A geometry device may override with its own "endpoints" (e.g. a Yosemite sled
# with 4 cards: {"bmc": 1, "hosts": 4}).
KIND_ENDPOINTS = {"compute-tp": {"bmc": 1, "hosts": 1},
                  "bc-head-tp": {"bmc": 1, "hosts": 1},
                  "bc-jbod": {"ioms": 2},
                  # lab racks (howlin): a head's kind names the enclosure it drives; its
                  # platform (Leopard / Tioga Pass) is the BMC FRU's, not the kind's
                  "compute-leopard": {"bmc": 1, "hosts": 1},
                  "bb-head": {"bmc": 1, "hosts": 1},
                  "lightning-head": {"bmc": 1, "hosts": 1},
                  "bc-head": {"bmc": 1, "hosts": 1},
                  "bigbasin": {"bmc": 1},
                  "lightning": {"bmc": 1},
                  "yv2": {"bmc": 1, "hosts": 4},
                  "yv3": {"bmc": 1, "hosts": 9}}       # sled model: a later plan
_EP_KEYS = ("hosts", "bmc", "ioms")
# head kind -> the kind of enclosure it drives (Device.drives names that enclosure's pos);
# the enclosure is judged through the head's PCIe (Qualify enclosure-check)
HEAD_KINDS = {"bb-head": "bigbasin", "lightning-head": "lightning", "bc-head": "bc-jbod"}
# which BryceCanyon IOM each iomN link is cabled to: `check_pal_sku.sh` reports it as
# "IOM Location: Side A|B". A device may set its own {"sides": {"iom1": "A", ...}}.
KIND_SIDES = {"bc-jbod": {"iom1": "A", "iom2": "B"}}
IOM_SIDES = ("A", "B")
# a switch's own endpoints on the site mgmt network (the turtle port in site_port):
# discovered by MAC + current lease, never reserved (vid 26 is a dynamic pool)
SWITCH_ENDPOINTS = {"wedge100s": {"sonic": 1, "bmc": 1}, "dell-n3248te": {"sonic": 1}}
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
    # where the switch's own mgmt port lands on the site: {"switch": "turtle-gouda",
    # "port": "Gi1/0/18"} (switch = a name in /etc/flax/switches.json)
    site_port: dict | None = None

    @property
    def endpoints(self) -> dict[str, int]:
        return dict(SWITCH_ENDPOINTS.get(self.model, {}))


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
    sides: dict[str, str] = field(default_factory=dict)       # {"iom1": "A", ...} expected
    drives: str | None = None       # a head: the enclosure pos it drives (HEAD_KINDS)
    expect: dict = field(default_factory=dict)   # an enclosure: {"gpu": 8} / {"nvme": 4} / {"disks": 4}
    placed: dict | None = None      # lab racks: the operator's placement (with_placements)
    integration: bool = True        # the rack's integration_tests (rack-wide rungs: iperf mesh)


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
    # a lab standup rack (howlin): full-BOM geometry, hardware placed by the operator,
    # Snapshot instead of Close; integration_tests False drops the rack-wide rungs (HA,
    # iperf mesh) -- default: on for an order, off for a lab rack
    lab: bool = False
    integration_tests: bool = True

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


def parse_sides(kind: str, sides, n_ioms: int, ctx: str) -> dict[str, str]:
    """The IOM side each iomN must report (`check_pal_sku.sh` IOM Location): the device's
    own "sides", else the kind's default, limited to the IOMs the device has."""
    if sides is None:
        sides = KIND_SIDES.get(kind, {})
    want = {f"iom{n}" for n in range(1, n_ioms + 1)}
    if not isinstance(sides, dict) or set(sides) - want or \
            any(v not in IOM_SIDES for v in sides.values()):
        raise GeometryError(f"{ctx}: bad sides {sides!r} (want {{\"iom1\": \"A\", "
                            f"\"iom2\": \"B\"}} for its {n_ioms} IOM(s))")
    return {k: v for k, v in sides.items() if k in want}


def parse_site_port(sp, ctx: str) -> dict | None:
    if sp is None:
        return None
    if (not isinstance(sp, dict) or set(sp) != {"switch", "port"}
            or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._-]{0,63}", v)
                       for v in sp.values())):
        raise GeometryError(f"{ctx}: bad site_port {sp!r} "
                            "(want {\"switch\": \"turtle-gouda\", \"port\": \"Gi1/0/18\"})")
    return dict(sp)


def parse_expect(ex, ctx: str) -> dict:
    """What an enclosure must show through its head: {"gpu"|"nvme"|"disks": n}."""
    if ex is None:
        return {}
    if (not isinstance(ex, dict) or set(ex) - {"gpu", "nvme", "disks"}
            or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in ex.values())):
        raise GeometryError(f"{ctx}: bad expect {ex!r} (keys gpu / nvme / disks, ints)")
    return dict(ex)


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
            uplink_ports=[int(p) for p in s.get("uplink_ports", [])],
            site_port=parse_site_port(s.get("site_port"), f"switch {s.get('id')}")))

    devices = []
    seen = set()
    mgmt = {s.id for s in switches if s.role == "mgmt"}
    ports: dict[Link, str] = {}       # mgmt (switch, port, lane) -> "pos link"
    for x in data.get("devices", []):
        pos = _req(x, "pos", "device")
        if not isinstance(pos, str) or not _POS_RE.fullmatch(pos):
            raise GeometryError(f"bad device pos {pos!r} (want [A-Za-z0-9][A-Za-z0-9_-]{{0,31}})")
        kind = _req(x, "kind", pos)
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
            endpoints=endpoints,
            sides=parse_sides(kind, x.get("sides"), endpoints["ioms"], pos),
            drives=x.get("drives"), expect=parse_expect(x.get("expect"), pos)))
    kinds = {d.pos: d.kind for d in devices}
    lab = data.get("lab", False)
    integ = data.get("integration_tests", not lab)
    for d in devices:
        d.integration = integ
    for d in devices:
        if d.drives is None:
            continue
        want = HEAD_KINDS.get(d.kind)
        if want is None:
            raise GeometryError(f"{d.pos}: a {d.kind} drives nothing (drives {d.drives!r})")
        if kinds.get(d.drives) != want:
            raise GeometryError(f"{d.pos}: drives {d.drives!r} must name a {want} "
                                f"(got {kinds.get(d.drives) or 'no such device'})")
    if not isinstance(lab, bool) or not isinstance(integ, bool):
        raise GeometryError("lab / integration_tests must be true or false")

    return Geometry(
        order=order, customer=_req(data, "customer", "geometry"),
        site=_req(data, "site", "geometry"), rack=rack, prefix=prefix,
        vid=int(_req(net, "vid", "network")), cidr=_req(net, "cidr", "network"),
        iperf_server=data.get("iperf_server"), switches=switches, devices=devices,
        lab=lab, integration_tests=integ)


# the hardware family (BMC FRU, flax_post family-map) each slot kind is built from;
# another family placed there is a deviation (shown, recorded, never refused)
KIND_FAMILIES = {"compute-tp": {"tiogapass"}, "bc-head-tp": {"tiogapass"},
                 "compute-leopard": {"leopard"}, "bb-head": {"tiogapass", "leopard"},
                 "lightning-head": {"tiogapass", "leopard"}, "bc-head": {"tiogapass", "leopard"}}


def deviation(d: Device, placed: dict | None) -> str | None:
    """'kind: leopard in a compute-tp slot' when the placed hardware's family is not one the
    slot's kind is built from; None when it is (or nothing is placed / no family read)."""
    fam = (placed or {}).get("family")
    want = KIND_FAMILIES.get(d.kind)
    if not fam or want is None or fam in want:
        return None
    return f"kind: {fam} in a {d.kind} slot"


def with_placements(g: Geometry, placements: dict[str, dict]) -> Geometry:
    """The effective geometry of a lab rack: each placed device gets the switch port its
    hardware was seen on as its mezz0 link (an IOM key "<pos>.iomN" as that device's iomN
    link) and carries its placement; unplaced devices are unchanged. The committed
    geometry is not modified."""
    devs = []
    for d in g.devices:
        links, placed = dict(d.links), placements.get(d.pos)
        if placed:
            links["mezz0"] = Link(placed["switch"], int(placed["port"]),
                                  placed.get("lane") and int(placed["lane"]))
        for k, p in placements.items():
            if k.startswith(d.pos + ".iom"):
                links[k.split(".", 1)[1]] = Link(p["switch"], int(p["port"]),
                                                 p.get("lane") and int(p["lane"]))
        devs.append(replace(d, links=links, placed=placed) if links != d.links or placed
                    else d)
    return replace(g, devices=devs)


def absent(g: Geometry, placements: dict[str, dict]) -> set[str]:
    """Lab racks: the BOM positions no hardware has been placed into (an IOM placement
    "<pos>.iomN" makes its JBOD present). An order (lab False) has no absent devices."""
    if not g.lab:
        return set()
    have = {k.split(".", 1)[0] for k in placements}
    return {d.pos for d in g.devices if d.pos not in have}


def load_geometry(path) -> Geometry:
    with open(path) as f:
        return parse_geometry(json.load(f))
