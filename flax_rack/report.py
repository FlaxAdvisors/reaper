"""Done's customer report: two CSVs in the order dir, machine-readable, stable columns.

customer-devices.csv     one row per geometry device (pos), per JBOD IOM (<pos>.iom<n>)
                         and per switch (_sw-<id>), DEVICE_COLUMNS.
customer-components.csv  latest_components: the parts of each device's LATEST Qualify
                         inventory (macinv -v; order.db's rows when Done's `components`
                         rung read that same run), COMPONENT_COLUMNS.

Every cell comes from that row's own device and its LATEST run of the rung named
(order.db latest_runs; never an older run, never another device): a device that never
ran a rung has "" there, not a neighbour's value. Sources:
  chassis_serial      Post's FRU ID 0 ship serial in the latest inventory's macinv-v
  bmc_mac / host_mac  Discover's MACs (order.db device; IOM n = iom_macs[n-1]; a
                      switch's from its latest passed bmc-leased)
  fabric_*            the latest passed Deploy ha-config (bond0 MACs per wedge side,
                      the A-side address); a device without one: the MAC of the port
                      Qualify's latest ha-path saw on each wedge (inventory ip-d-link),
                      no address
  *_version           the latest Firmware <x>-checked slice's current version
  qualify / deploy    qualify.qualify_why / deploy.deploy_why: pass, fail, or
                      unmeasured ("not measured: ..."); "" before the stage ran
  ha_mode             order.db ha_mode's mode for a device whose ha-test passed
  done                the worst latest Done-stage rung: fail > unmeasured > pass > skip
Written with the csv module (quotes/commas in model strings round-trip), each file
atomically replaced.
"""
import csv
import os
import re
from pathlib import Path

from flax_post import inventory
from flax_rungs import bond

from .addressing import plan_addresses
from .deploy import deploy_rungs, deploy_why
from .discover_ladder import DISCOVER_STEPS
from .qualify import device_rungs, parse_ip_link, qualify_why

DEVICE_COLUMNS = ("order", "rack", "pos", "ou", "kind", "part", "hostname",
                  "chassis_serial", "bmc_mac", "host_mac", "fabric_a_mac", "fabric_b_mac",
                  "fabric_a_ip", "bmc_version", "bios_version", "nic_firmware", "qualify",
                  "deploy", "ha_mode", "done")
COMPONENT_COLUMNS = ("order", "rack", "pos", "kind", "model", "serial", "slot")
DEVICES_CSV = "customer-devices.csv"
COMPONENTS_CSV = "customer-components.csv"
INVENTORY_RUNG, INVENTORY_ART = "inventory", "macinv-v"
DONE_STAGE = "done"
_DONE_RANK = ("fail", "unmeasured", "pass", "skip")

_BUS_RE = re.compile(r"^(?:Handler:\s*\d+,\s*)?BusID:\s*(?P<bus>[^,\s]+)")
_NIC_SN_RE = re.compile(r"^BusID:\s*(?P<bus>[^,\s]+),\s*\[SN\] Serial number:\s*(?P<sn>.*)$")


# ---------------------------------------------------------------- components
def components_from_inventory(verbose_text: str) -> list[dict]:
    """macinv -v detail text -> [{kind cpu|dimm|nic|disk|gpu, model, serial, slot}], in
    the order macinv printed them. Values are flax_post.inventory.parse's; slots are
    CPU<n>, the DIMM label, the PCI BusID (nic, gpu) or the block BusID (disk). A part
    without a serial keeps serial "" (never filled from anywhere else)."""
    inv = inventory.parse(verbose_text or "", family_map={})
    lines = [ln.strip() for ln in (verbose_text or "").splitlines()]
    bus = lambda ln: (_BUS_RE.match(ln) or {"bus": ""})["bus"]     # noqa: E731
    # the BusIDs of the lines parse() turned into nic / storage / gpu entries, same order
    nic_bus = [bus(ln) for ln in lines if inventory._NIC_RE.match(ln)]
    disk_bus = [bus(ln) for ln in lines if inventory._STORAGE_LINE_RE.match(ln)
                and inventory._STORAGE_MODEL_RE.search(ln)]
    gpu_bus = [bus(ln) for ln in lines
               if inventory._GPU_RE.match(ln) and not inventory._NIC_RE.match(ln)]
    nic_sn = {m["bus"]: m["sn"].strip() for m in map(_NIC_SN_RE.match, lines) if m}
    at = lambda xs, i: xs[i] if i < len(xs) else ""                 # noqa: E731

    out = []
    for i, c in enumerate(inv["cpu"]):
        out.append({"kind": "cpu", "model": c["model"], "serial": c["serial"],
                    "slot": f"CPU{i}"})
    for m in inv["memory"]:
        out.append({"kind": "dimm", "model": m["part"], "serial": m["serial"],
                    "slot": m["slot"]})
    for i, n in enumerate(inv["nic"]):
        b = at(nic_bus, i)
        out.append({"kind": "nic", "model": n["model"], "serial": nic_sn.get(b, ""),
                    "slot": b})
    for i, s in enumerate(inv["storage"]):
        out.append({"kind": "disk", "model": s["model"], "serial": s["serial"],
                    "slot": at(disk_bus, i)})
    for i, gp in enumerate(inv["gpu"]):
        out.append({"kind": "gpu", "model": gp["model"], "serial": "", "slot": at(gpu_bus, i)})
    return out


def latest_components(order_dir, db, lr=None) -> list[dict]:
    """[{pos, kind, model, serial, slot}] of every pos with a Qualify inventory run, from
    its LATEST inventory: order.db's rows when Done read them from that run (source_run),
    else parsed again from that run's macinv-v (no artifact: none). Never an older
    run's parts beside the latest run's chassis serial (a blade reseated and
    re-qualified after Done, or Done never run)."""
    lr = db.latest_runs() if lr is None else lr
    out = []
    for pos in sorted({k[0] for k in lr if k[1] == INVENTORY_RUNG}):
        rid = db.latest_run_id(pos, INVENTORY_RUNG)
        rows = db.components(pos)
        if not rows or any(r["source_run"] != rid for r in rows):
            text = inventory_text(order_dir, lr, pos)
            rows = components_from_inventory(text) if text else []
        out += [{"pos": pos, **{k: r[k] for k in ("kind", "model", "serial", "slot")}}
                for r in rows]
    return out


def inventory_text(order_dir, lr, pos) -> str | None:
    """The latest Qualify inventory run's macinv-v artifact for pos (None: no such run,
    no artifact, or unreadable)."""
    rel = ((lr.get((pos, INVENTORY_RUNG)) or {}).get("artifacts") or {}).get(INVENTORY_ART)
    if not rel:
        return None
    try:
        return (Path(order_dir) / rel).read_text()
    except OSError:
        return None


# ---------------------------------------------------------------- device rows
def _mac(h: str | None) -> str:
    """order.db's 12-hex MAC as aa:bb:cc:dd:ee:ff ("" for none)."""
    h = re.sub(r"[^0-9a-fA-F]", "", h or "").lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ""


def _fabric_macs(order_dir, lr, pos) -> dict[str, str]:
    """{"A": mac, "B": mac}: the bond0 MACs of the latest passed Deploy ha-config, else
    the ports Qualify's latest ha-path saw on each wedge, MAC'd by the latest inventory's
    ip-d-link (a device that never deployed). A side with no such evidence is absent."""
    hac = _done(lr, pos, "ha-config")
    if hac.get("verdict") == "pass":
        return {k: v for k, v in ((hac.get("detail") or {}).get("macs") or {}).items() if v}
    links = (_done(lr, pos, "ha-path").get("detail") or {}).get("links") or []
    rel = ((lr.get((pos, INVENTORY_RUNG)) or {}).get("artifacts") or {}).get("ip-d-link")
    if not links or not rel:
        return {}
    try:
        ifmac = parse_ip_link((Path(order_dir) / rel).read_text())
    except OSError:
        return {}
    side, _err = bond.sides(links, ifmac)
    macs = {k: v["mac"] for k, v in side.items()}
    return {} if macs.get("A") and macs.get("A") == macs.get("B") else macs


def _done(lr, key, rung):
    r = lr.get((key, rung)) or {}
    return r if r.get("status") == "done" else {}


def _from_why(why: str | None) -> str:
    if why is None:
        return "pass"
    return "unmeasured" if why.startswith("not measured") else "fail"


def _fw(lr, key, prefix) -> str:
    sl = (_done(lr, key, f"{prefix}-checked").get("detail") or {}).get("slice") or {}
    if prefix == "mlx":
        cur = []
        for d in sl.get("devices") or []:
            if d.get("current") and d["current"] not in cur:
                cur.append(d["current"])
        return " ".join(cur)
    return str(sl.get("current") or sl.get("current_version") or "")


def _row_value(detail, label) -> str:
    return next((str(v) for k, v in (detail or {}).get("rows") or [] if k == label), "")


def _stage_ran(lr, key, stage) -> bool:
    return any(k[0] == key and r.get("stage") == stage for k, r in lr.items())


def _done_verdict(lr, key) -> str:
    vs = {r.get("verdict") for k, r in lr.items()
          if k[0] == key and r.get("stage") == DONE_STAGE and r.get("status") == "done"}
    return next((v for v in _DONE_RANK if v in vs), "")


def _blank(**kw) -> dict:
    return {c: kw.get(c, "") for c in DEVICE_COLUMNS}


def device_rows(order_dir, g, db) -> list[dict]:
    """DEVICE_COLUMNS dicts: devices in geometry order, each JBOD's IOMs after it, then
    the switches."""
    lr = db.latest_runs()
    rows_db = {x["pos"]: x for x in db.devices()}
    ha = bond.clean_ha(db.ha_mode() or {})
    names: dict[tuple, str] = {(a.pos, a.role): a.name for a in plan_addresses(g)}
    qwhy, dwhy = qualify_why(lr, g), deploy_why(lr, g)
    out = []
    for d in g.devices:
        row = rows_db.get(d.pos) or {}
        inv = inventory_text(order_dir, lr, d.pos)
        fru = (inventory.parse(inv).get("fru") or {}) if inv else {}
        if device_rungs(d):
            qualify = _from_why(qwhy.get(d.pos)) if _stage_ran(lr, d.pos, "qualify") else ""
        else:       # an IOM-only device qualifies by its Discover ladder
            ran = any((d.pos, s) in lr for s in DISCOVER_STEPS)
            qualify = _from_why(qwhy.get(d.pos)) if ran else ""
        dr = deploy_rungs(d)
        deploy = (_from_why(dwhy.get(d.pos)) if any((d.pos, r) in lr for r in dr) else "")
        hac = _done(lr, d.pos, "ha-config")
        fab = (hac.get("detail") or {}) if hac.get("verdict") == "pass" else {}
        fmac = _fabric_macs(order_dir, lr, d.pos)
        hosts = [names[k] for k in sorted(names) if k[0] == d.pos and k[1].startswith("host")]
        out.append(_blank(
            order=g.order, rack=g.rack, pos=d.pos, ou=d.ou, kind=d.kind, part=d.part,
            hostname=" ".join(hosts), chassis_serial=fru.get("serial") or "",
            bmc_mac=_mac(row.get("bmc_mac")), host_mac=_mac(row.get("host_mac")),
            fabric_a_mac=fmac.get("A", ""), fabric_b_mac=fmac.get("B", ""),
            fabric_a_ip=(fab.get("address") or "").split("/")[0],
            bmc_version=_fw(lr, d.pos, "bmc"), bios_version=_fw(lr, d.pos, "bios"),
            nic_firmware=_fw(lr, d.pos, "mlx"), qualify=qualify, deploy=deploy,
            ha_mode=ha.get("mode", "") if d.pos in (ha.get("devices") or []) else "",
            done=_done_verdict(lr, d.pos)))
        ioms = row.get("iom_macs") or []
        for n in range(1, d.endpoints.get("ioms", 0) + 1):
            key = f"{d.pos}.iom{n}"
            out.append(_blank(
                order=g.order, rack=g.rack, pos=key, ou=d.ou, kind="iom", part=d.part,
                hostname=names.get((d.pos, f"iom{n}"), ""),
                bmc_mac=_mac(ioms[n - 1] if n <= len(ioms) else None),
                bmc_version=_fw(lr, key, "bmc"), done=_done_verdict(lr, key)))
    for s in g.switches:
        key = f"_sw-{s.id}"
        leased = _done(lr, key, "bmc-leased")
        out.append(_blank(
            order=g.order, rack=g.rack, pos=key, ou=s.ou, kind=f"switch-{s.role}",
            part=s.model,
            bmc_mac=(_mac(_row_value(leased.get("detail"), "BMC MAC"))
                     if leased.get("verdict") == "pass" else ""),
            bmc_version=_fw(lr, key, "bmc"), done=_done_verdict(lr, key)))
    return out


# ---------------------------------------------------------------- write
def _write_csv(path: Path, columns, rows) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
    os.replace(tmp, path)


def write(order_dir: Path, g, db) -> dict[str, Path]:
    """Both CSVs into order_dir (atomic replace). -> {"devices": path, "components": path}."""
    order_dir = Path(order_dir)
    devs = order_dir / DEVICES_CSV
    comps = order_dir / COMPONENTS_CSV
    _write_csv(devs, DEVICE_COLUMNS, device_rows(order_dir, g, db))
    _write_csv(comps, COMPONENT_COLUMNS,
               [{"order": g.order, "rack": g.rack, **c}
                for c in latest_components(order_dir, db)])
    return {"devices": devs, "components": comps}
