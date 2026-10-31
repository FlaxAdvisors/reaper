"""Done's customer report: three CSVs in the order dir, machine-readable, stable columns.
Every one lists the rack top down: highest OU first, then column L, C, R; a JBOD's IOMs
after it by side (A, B); no order column (it is in the file name), a `rack` column only
when the order has a rack tag (multi-rack), no hostnames.

Named <customer>-<order>[-<rack>]-<kind>.csv (geometry customer + order, + the rack tag
when there is one, filename-safe: csv_names); before 2026-10-31 they were customer-{devices,firmware,components}.csv, which
csv_path still reads (and Start fresh still renames).

<c>-<o>-devices.csv      PHYSICAL: one row per geometry device (pos), per JBOD IOM
                         (<pos>.iom<n>) and per switch (_sw-<id>), DEVICE_COLUMNS.
<c>-<o>-firmware.csv     FW + QUAL: the same rows, FIRMWARE_COLUMNS.
<c>-<o>-inventory.csv    the parts of each device's LATEST Qualify inventory (macinv -v;
                         order.db's rows when Done's `components` rung read that same
                         run), COMPONENT_COLUMNS. No BMC images (they are firmware).

Every cell comes from that row's own device and its LATEST run of the rung named
(order.db latest_runs; never an older run, never another device): a device that never
ran a rung has "" there, not a neighbour's value. Sources:
  serial              a node: Post's FRU ID 0 ship serial in the latest inventory's
                      macinv-v; an IOM (board serial) / switch (syseeprom / weutil): its
                      latest Discover `serial` read
  kind                geometry's; an IOM `iom A (left/front)` / `iom B (right/back)` by
                      the side its latest `serial` read reported (check_pal_sku.sh)
  bmc_mac / host_mac  Discover's MACs (order.db device; IOM n = iom_macs[n-1]; a switch:
                      bmc = its latest passed bmc-leased, host = the mgmt MAC its latest
                      passed host-mac-seen saw: what DHCP sees)
  fabric_*_mac        the latest passed Deploy ha-config (bond0 MACs per wedge side); a
                      device without one: the MAC of the port Qualify's latest ha-path
                      saw on each wedge (inventory ip-d-link)
  *_port              FRONT-PANEL `<switch id>:<port>[/<lane>]` as geometry writes links
                      (never SONiC EthernetN): mgmt = the link to the mgmt switch,
                      fabric_a/b = the link to wedge A/B. AS-BUILT: when the live link
                      monitor's fresh snapshot sees the device (its LLDP name, its MACs;
                      an IOM link its own MAC) on exactly one other port of that switch
                      and not on the geometry port, that port. A switch: mgmt = its site
                      port (`turtle-gouda:Gi1/0/19`, as-built from its eth0 LLDP),
                      fabric_a = its uplink ports.
  *_version           the latest Firmware <x>-checked slice's current version;
                      bmc_rom_version a node's backup BMC chip / an IOM's ROM Version (the
                      backup step's read, "" unless it passed)
  qualify / deploy    qualify.qualify_why / deploy.deploy_why: pass, fail, or
                      unmeasured ("not measured: ..."); "" before the stage ran
  ha_mode             order.db ha_mode's mode for a device whose ha-test passed
  done                the worst latest Done-stage rung: fail > unmeasured > pass > skip

device_rows is the archive's row set (archive.py: hostname, chassis_serial, kind `iom`;
geometry order) -- the customer files are built from it, never the other way round.
Written with the csv module (quotes/commas in model strings round-trip), each file
atomically replaced.
"""
import csv
import os
import re
from pathlib import Path

from flax_post import inventory
from flax_rungs import bond

from . import linkmon
from .addressing import plan_addresses
from .deploy import deploy_rungs, deploy_why
from .discover_ladder import DISCOVER_STEPS
from .firmware import BACKUP_STEP
from .geometry import IOM_SIDES, Link
from .qualify import device_rungs, parse_ip_link, qualify_why

# the customer files ("rack" is dropped when the order has no rack tag)
DEVICE_COLUMNS = ("rack", "pos", "ou", "kind", "part", "serial", "bmc_mac", "host_mac",
                  "fabric_a_mac", "fabric_b_mac", "mgmt_port", "fabric_a_port",
                  "fabric_b_port")
FIRMWARE_COLUMNS = ("rack", "pos", "ou", "kind", "serial", "bios_version", "bmc_version",
                    "bmc_rom_version", "nic_firmware", "qualify", "deploy", "ha_mode", "done")
COMPONENT_COLUMNS = ("rack", "pos", "kind", "model", "serial", "slot")
# device_rows' keys (the archive's rows: archive.py)
ROW_KEYS = ("order", "rack", "pos", "ou", "kind", "part", "hostname",
            "chassis_serial", "bmc_mac", "host_mac", "fabric_a_mac", "fabric_b_mac",
            "bmc_version", "bios_version", "nic_firmware",
            "qualify", "deploy", "ha_mode", "done")
CSV_KINDS = ("devices", "firmware", "inventory")
# the names before 2026-10-31 (read, never written)
LEGACY_CSV = {"devices": "customer-devices.csv", "firmware": "customer-firmware.csv",
              "inventory": "customer-components.csv"}
# an order dir's current CSVs (not a pass's *.pass<N>.csv): Start fresh renames these
CSV_RE = re.compile(r"(?:.+-(?:devices|firmware|inventory)"
                    r"|customer-(?:devices|firmware|components))\.csv")
IOM_KIND = {"A": "iom A (left/front)", "B": "iom B (right/back)"}
INVENTORY_RUNG, INVENTORY_ART = "inventory", "macinv-v"
DONE_STAGE = "done"
_DONE_RANK = ("fail", "unmeasured", "pass", "skip")

_BUS_RE = re.compile(r"^(?:Handler:\s*\d+,\s*)?BusID:\s*(?P<bus>[^,\s]+)")
_NIC_SN_RE = re.compile(r"^BusID:\s*(?P<bus>[^,\s]+),\s*\[SN\] Serial number:\s*(?P<sn>.*)$")


def csv_names(g) -> dict[str, str]:
    """{kind: "<customer>-<order>[-<rack>]-<kind>.csv"}, filename-safe (the rack tag keeps
    two racks of one order apart)."""
    tag = f"{g.customer or 'customer'}-{g.order}" + (f"-{g.rack}" if g.rack else "")
    tag = re.sub(r"[^A-Za-z0-9._-]+", "_", tag).strip("_")
    return {k: f"{tag}-{k}.csv" for k in CSV_KINDS}


def csv_path(order_dir, g, kind) -> Path | None:
    """The CSV of `kind` Done last wrote in order_dir (the old name as a fallback), or None."""
    for name in (csv_names(g)[kind], LEGACY_CSV[kind]):
        p = Path(order_dir) / name
        if p.is_file():
            return p
    return None


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


def _from_why(why: str | None, failed: bool = False) -> str:
    """qualify_why / deploy_why's reason as the CSV's word. "fail" only for a real
    failure (failed: one of the stage's rungs failed, though an earlier one never ran);
    "" when the first hole is a rung not run (no verdict to report)."""
    if why is None:
        return "pass"
    if failed:
        return "fail"
    if why.startswith("not measured"):
        return "unmeasured"
    return "" if why.endswith(" not run") else "fail"


def _failed(lr, key, rungs) -> bool:
    return any((lr.get((key, r)) or {}).get("verdict") == "fail" for r in rungs)


def _fw(lr, key, prefix) -> str:
    sl = (_done(lr, key, f"{prefix}-checked").get("detail") or {}).get("slice") or {}
    if prefix == "mlx":
        cur = []
        for d in sl.get("devices") or []:
            if d.get("current") and d["current"] not in cur:
                cur.append(d["current"])
        return " ".join(cur)
    return str(sl.get("current") or sl.get("current_version") or "")


def _backup(lr, key) -> str:
    """The backup BMC version this pass's Firmware read ("" unless it passed)."""
    r = _done(lr, key, BACKUP_STEP)
    return str((r.get("detail") or {}).get("backup_version") or "") \
        if r.get("verdict") == "pass" else ""


def _row_value(detail, label) -> str:
    return next((str(v) for k, v in (detail or {}).get("rows") or [] if k == label), "")


def _stage_ran(lr, key, stage) -> bool:
    return any(k[0] == key and r.get("stage") == stage for k, r in lr.items())


def _done_verdict(lr, key) -> str:
    vs = {r.get("verdict") for k, r in lr.items()
          if k[0] == key and r.get("stage") == DONE_STAGE and r.get("status") == "done"}
    return next((v for v in _DONE_RANK if v in vs), "")


def _blank(**kw) -> dict:
    return {c: kw.get(c, "") for c in ROW_KEYS}


def device_rows(order_dir, g, db) -> list[dict]:
    """ROW_KEYS dicts (the archive's rows): devices in geometry order, each JBOD's IOMs
    after it, then the switches."""
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
            qualify = (_from_why(qwhy.get(d.pos), _failed(lr, d.pos, device_rungs(d)))
                       if _stage_ran(lr, d.pos, "qualify") else "")
        else:       # an IOM-only device qualifies by its Discover ladder
            ran = any((d.pos, s) in lr for s in DISCOVER_STEPS)
            qualify = _from_why(qwhy.get(d.pos), _failed(lr, d.pos, DISCOVER_STEPS)) \
                if ran else ""
        dr = deploy_rungs(d)
        deploy = (_from_why(dwhy.get(d.pos), _failed(lr, d.pos, dr))
                  if any((d.pos, r) in lr for r in dr) else "")
        fmac = _fabric_macs(order_dir, lr, d.pos)
        hosts = [names[k] for k in sorted(names) if k[0] == d.pos and k[1].startswith("host")]
        out.append(_blank(
            order=g.order, rack=g.rack, pos=d.pos, ou=d.ou, kind=d.kind, part=d.part,
            hostname=" ".join(hosts), chassis_serial=fru.get("serial") or "",
            bmc_mac=_mac(row.get("bmc_mac")), host_mac=_mac(row.get("host_mac")),
            fabric_a_mac=fmac.get("A", ""), fabric_b_mac=fmac.get("B", ""),
            bmc_version=_fw(lr, d.pos, "bmc"),
            bios_version=_fw(lr, d.pos, "bios"),
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
                bmc_version=_fw(lr, key, "bmc") or str(
                    ((_done(lr, key, "bmc-checked").get("detail") or {}).get("slice") or {})
                    .get("versions", {}).get("BMC Version") or ""),
                done=_done_verdict(lr, key)))
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


# ---------------------------------------------------------------- customer rows
def columns(g, cols) -> tuple:
    """A customer file's header: no `rack` column for an order without a rack tag."""
    return cols if g.rack else tuple(c for c in cols if c != "rack")


def _port(l: Link | None) -> str:
    return f"{l.switch}:{l.port}" + (f"/{l.lane}" if l.lane else "") if l else ""


def _front(sw, iface: str, lane_hint) -> Link | None:
    """SONiC EthernetN on switch sw -> its front-panel Link (a wedge: 4 lanes a port; the
    lane is written when the geometry link had one or it is not lane 1)."""
    m = re.fullmatch(r"Ethernet(\d+)", iface or "")
    if not m:
        return None
    n = int(m[1])
    if sw.model == "wedge100s":
        port, lane = divmod(n, 4)
        return Link(sw.id, port + 1, lane + 1 if lane_hint is not None or lane else None)
    return Link(sw.id, n + 1, None)


def _fresh(snaps, sid, now) -> dict | None:
    snap = (snaps or {}).get(sid)
    if not snap or snap.get("at") is None or \
            (now is not None and now - snap["at"] > linkmon.STALE_S):
        return None
    return snap


def _as_built(d, name, l, sws, snaps, now, macs, base) -> Link:
    """The geometry link, or the one other port of that switch the fresh live snapshot
    sees this device on (and not on the geometry port): wrong cabling, as built."""
    sw, snap = sws.get(l.switch), _fresh(snaps, l.switch, now)
    if sw is None or snap is None:
        return l
    if not name.startswith("iom") and \
            sum(x.switch == l.switch for x in d.links.values()) > 1:
        return l                            # two links on one switch: not told apart
    up = {linkmon.link_iface(sw, p, None) for p in sw.uplink_ports}
    seen = set()
    for e in snap.get("fdb") or []:
        e = linkmon._d(e)
        if (e.get("mac") or "").replace(":", "").lower() in macs:
            seen.add(e.get("iface"))
    if not name.startswith("iom"):          # an IOM link: its own MAC only
        for e in snap.get("lldp") or []:
            e = linkmon._d(e)
            n = (e.get("remote_device") or "").strip().lower()
            if n == base or n.startswith(base + "-"):
                seen.add(e.get("local_iface"))
    seen -= up | {"eth0", None}
    if linkmon.link_iface(sw, l.port, l.lane) in seen or len(seen) != 1:
        return l
    return _front(sw, seen.pop(), l.lane) or l


def _site_port(s, snaps, now) -> str:
    """A switch's mgmt port on the site: geometry site_port, else (as built) the one
    other eth0 LLDP neighbour its fresh snapshot sees."""
    if not s.site_port:
        return ""
    want = f"{s.site_port['switch']}:{s.site_port['port']}"
    snap = _fresh(snaps, s.id, now)
    if snap is None or linkmon.site_link(s, snap, True, now)["state"] != "wrong":
        return want
    nbs = [linkmon._d(e) for e in snap.get("lldp") or []
           if linkmon._d(e).get("local_iface") == "eth0"]
    if len(nbs) != 1 or not nbs[0].get("remote_device") or not nbs[0].get("remote_port"):
        return want
    return f"{nbs[0]['remote_device'].split('.')[0]}:{nbs[0]['remote_port']}"


def customer_rows(order_dir, g, db, snaps=None, now=None) -> list[dict]:
    """device_rows + the customer cells (serial, kind, bmc_rom_version, *_port), top
    down: highest OU first, then column (L, C, R), a JBOD's IOMs after it by side."""
    lr = db.latest_runs()
    sws = {s.id: s for s in g.switches}
    mgmt = {s.id for s in g.switches if s.role == "mgmt"}
    geo = {d.pos: d for d in g.devices}
    dbdev = {x["pos"]: x for x in db.devices()}
    names = linkmon.device_names(g)
    out = []
    for row in device_rows(order_dir, g, db):
        key = row["pos"]
        r = dict(row, serial=row["chassis_serial"], bmc_rom_version="",
                 mgmt_port="", fabric_a_port="", fabric_b_port="")
        ident = (_done(lr, key, "serial").get("detail") or {})
        if key.startswith("_sw-"):
            s = sws[key[4:]]
            seen = _done(lr, key, "host-mac-seen")
            r.update(serial=str(ident.get("serial") or ""),
                     host_mac=_mac((seen.get("detail") or {}).get("mac"))
                     if seen.get("verdict") == "pass" else "",
                     mgmt_port=_site_port(s, snaps, now),
                     fabric_a_port=" ".join(_port(Link(s.id, p, None)) for p in s.uplink_ports))
            sort = (-s.ou, 0, 0, 0)
        elif ".iom" in key:
            pos, n = key.rsplit(".iom", 1)
            d, side = geo[pos], ident.get("side")
            ioms = (dbdev.get(pos) or {}).get("iom_macs") or []
            l = d.links.get(f"iom{n}")
            mac = (ioms[int(n) - 1] if int(n) <= len(ioms) else "") or ""
            r.update(serial=str(ident.get("serial") or ""), kind=IOM_KIND.get(side, "iom"),
                     bmc_rom_version=_backup(lr, key),
                     mgmt_port=_port(l and _as_built(d, f"iom{n}", l, sws, snaps, now,
                                                    {mac.replace(":", "").lower()} - {""},
                                                    names[pos])))
            sort = (-d.ou, d.col or 0, 1,
                    IOM_SIDES.index(side) if side in IOM_SIDES else len(IOM_SIDES), int(n))
        else:
            d = geo[key]
            x = dbdev.get(key) or {}
            macs = {m for m in (x.get("host_mac"), x.get("bmc_mac")) if m}
            port = {}
            for name, l in d.links.items():
                if name.startswith("iom"):
                    continue
                col = ("mgmt_port" if l.switch in mgmt else
                       "fabric_a_port" if l.switch == "A" else
                       "fabric_b_port" if l.switch == "B" else None)
                if col and col not in port:
                    port[col] = _port(_as_built(d, name, l, sws, snaps, now, macs,
                                                 names[key]))
            r.update(port, bmc_rom_version=_backup(lr, key) if d.endpoints.get("hosts") else "")
            sort = (-d.ou, d.col or 0, 0, 0, 0)
        out.append((sort, r))
    out.sort(key=lambda x: x[0])            # stable: geometry order within a tie
    return [r for _s, r in out]


# ---------------------------------------------------------------- write
def _write_csv(path: Path, columns, rows) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
    os.replace(tmp, path)


def write(order_dir: Path, g, db, snaps: dict | None = None,
          now: float | None = None) -> dict[str, Path]:
    """The three CSVs into order_dir (atomic replace). snaps / now: the rack's live link
    monitor (LinkPoller.snapshots(), its clock; None = geometry ports only).
    -> {"devices": path, "firmware": path, "inventory": path} (csv_names)."""
    order_dir = Path(order_dir)
    names = csv_names(g)
    rows = customer_rows(order_dir, g, db, snaps, now)
    out = {}
    for key, cols in (("devices", DEVICE_COLUMNS), ("firmware", FIRMWARE_COLUMNS)):
        out[key] = order_dir / names[key]
        _write_csv(out[key], columns(g, cols), rows)
    rank = {r["pos"]: i for i, r in enumerate(rows)}
    parts = sorted(latest_components(order_dir, db), key=lambda c: rank.get(c["pos"], len(rank)))
    out["inventory"] = order_dir / names["inventory"]
    _write_csv(out["inventory"], columns(g, COMPONENT_COLUMNS),
               [{"rack": g.rack, **c} for c in parts])
    return out
