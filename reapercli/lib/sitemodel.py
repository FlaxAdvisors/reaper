"""The per-site device/link model (files/configs/<site>/devices.yml), the one source for
the site health board, its alert rules and the physical network diagrams (spec
2026-11-09-site-health-board-design). Our infrastructure only: bangs (with their BMC),
infra switches, the corp uplink. Never DUTs, switch DUTs or customer racks.

A link end is written "device:port" (the port as the device names it: Te1/1/1, Et29/3,
swp49, eth0). `expect: down` marks a link that is down on purpose (STP-blocked)."""
from dataclasses import dataclass
from pathlib import Path

import yaml

KINDS = ("bang", "switch", "uplink")
STATES = ("live", "planned", "offline", "dormant")
VENDOR_GROUPS = ("arista", "cisco", "cumulus")
BANG_GROUP = "jumphosts"


@dataclass(frozen=True)
class End:
    device: str
    port: str


@dataclass(frozen=True)
class Link:
    a: End
    b: End
    speed: str
    expect: str = "up"
    note: str = ""


@dataclass(frozen=True)
class Device:
    name: str
    kind: str
    mgmt_ip: str = ""
    vendor: str = ""
    state: str = "live"
    bmc_ip: str = ""
    note: str = ""


@dataclass(frozen=True)
class SiteModel:
    site: str
    devices: tuple
    links: tuple

    def device(self, name: str):
        return next((d for d in self.devices if d.name == name), None)


def _end(text: str) -> End:
    device, _, port = str(text).partition(":")
    return End(device.strip(), port.strip())


def load_model(root: Path, site: str) -> SiteModel:
    raw = yaml.safe_load((Path(root) / "files" / "configs" / site / "devices.yml").read_text()) or {}
    devices = tuple(Device(**{k: ("" if v is None else str(v)) for k, v in d.items()})
                    for d in raw.get("devices") or [])
    links = tuple(Link(_end(l["a"]), _end(l["b"]), str(l.get("speed", "")), str(l.get("expect", "up")),
                       str(l.get("note", ""))) for l in raw.get("links") or [])
    return SiteModel(str(raw.get("site", site)), devices, links)


def inventory_groups(root: Path, site: str) -> dict:
    """{host: group} for the vendor groups and jumphosts of inventories/<site>/hosts.yml."""
    inv = yaml.safe_load((Path(root) / "inventories" / site / "hosts.yml").read_text()) or {}
    out = {}

    def walk(node, group=None):
        for k, v in (node or {}).items():
            if k == "hosts":
                for h in (v or {}):
                    out.setdefault(h, group)
            elif k == "children":
                for g, sub in (v or {}).items():
                    walk(sub, g)
    walk(inv.get("all", inv))
    return {h: g for h, g in out.items() if g in VENDOR_GROUPS + (BANG_GROUP,)}


def dormant_hosts(root: Path, site: str) -> set:
    sites = (yaml.safe_load((Path(root) / "files" / "reaper" / "sites.yml").read_text()) or {}).get("sites", {})
    return set((sites.get(site) or {}).get("dormant_hosts") or ())


def validate(model: SiteModel, root: Path) -> list:
    """Every problem with the model as text; [] when sound."""
    errs, inv, dormant = [], inventory_groups(root, model.site), dormant_hosts(root, model.site)
    names = [d.name for d in model.devices]
    errs += [f"device {n} listed twice" for n in sorted({n for n in names if names.count(n) > 1})]
    for d in model.devices:
        if d.kind not in KINDS:
            errs.append(f"{d.name}: unknown kind {d.kind!r}")
        if d.state not in STATES:
            errs.append(f"{d.name}: unknown state {d.state!r}")
        if d.kind in ("bang", "switch"):
            if d.name not in inv:
                errs.append(f"{d.name}: not in inventories/{model.site}/hosts.yml")
            if not d.mgmt_ip:
                errs.append(f"{d.name}: no mgmt_ip")
        if d.kind == "switch" and d.name in inv and inv[d.name] != d.vendor:
            errs.append(f"{d.name}: vendor {d.vendor!r} but inventory group {inv[d.name]!r}")
        if (d.name in dormant) != (d.state == "dormant"):
            errs.append(f"{d.name}: state {d.state!r} but sites.yml dormant_hosts says "
                        f"{'dormant' if d.name in dormant else 'not dormant'}")
    errs += [f"{h}: in inventory ({g}) but not in devices.yml" for h, g in sorted(inv.items()) if h not in names]
    seen = {}
    for i, l in enumerate(model.links):
        for e in (l.a, l.b):
            if e.device not in names:
                errs.append(f"link {i}: unknown device {e.device!r}")
            if not e.port:
                errs.append(f"link {i}: {e.device} has no port")
            key = (e.device, e.port)
            if key in seen:
                errs.append(f"link {i}: {e.device}:{e.port} already used by link {seen[key]}")
            seen.setdefault(key, i)
        if l.expect not in ("up", "down"):
            errs.append(f"link {i}: expect {l.expect!r} (up|down)")
    return errs
