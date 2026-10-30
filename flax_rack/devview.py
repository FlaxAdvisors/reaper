"""Drawer data for /api/state: identity, firmware versions and switch port tables.

Read-only views over order.db's latest runs (never a device read): every switch, every
BMC endpoint (a TiogaPass BMC, each JBOD IOM, a wedge's BMC) gets its own

  identity  the Discover `serial` step's read: serial + the kind's fields (TiogaPass
            FRU 0, IOM `fruid-util iom`, wedge `weutil`, SONiC `show platform syseeprom`)
  fw        the Firmware reads: `bmc-checked`'s {component: version} for Facebook OpenBMC
            (fw-util / issue+cpld, report only) + not-present parts, and each manifest
            part's current/target/phase (TiogaPass BMC, BMC backup chip, BIOS, each
            Mellanox card)

and a switch its mgmt / BMC MAC+IP (Discover's *-mac-seen / *-leased) and a per-port
table: mgmt switch = FDB MACs per port -> the geometry device cabled there + link state
(the device's switchportlink read); a wedge = each geometry pcieN lane -> device +
the LLDP neighbour Qualify read on that interface.
"""
from flax_rungs.sonic import iface_port, wedge_iface


def _fmt(m):
    m = (m or "").replace(":", "").lower()
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else (m or None)


def _detail(run):
    return (run or {}).get("detail") or {}


def _row(detail, label):
    return next((r[1] for r in detail.get("rows") or [] if r and r[0] == label), None)


_ID_KEYS = ("serial_field", "product_name", "board_product", "board_serial", "part_number",
            "service_tag", "asset_tag", "local_mac", "ext_mac_base", "base_mac",
            "syseeprom_serial", "platform")


def identity(runs: dict) -> dict | None:
    """The `serial` step's identity read; None before Discover read one."""
    r = runs.get("serial")
    if not r or r.get("status") != "done":
        return None
    d = _detail(r)
    out = {"serial": d.get("serial"), "ok": r.get("verdict") == "pass",
           "headline": d.get("headline"), "rows": d.get("rows") or [],
           "artifacts": r.get("artifacts") or {}}
    out.update({k: d[k] for k in _ID_KEYS if d.get(k)})
    return out


def _part(name, sl):
    cur = sl.get("current") or sl.get("current_version")
    if not cur:
        return None
    return {"part": name, "current": cur, "target": sl.get("target") or sl.get("target_version"),
            "phase": sl.get("phase")}


def firmware(runs: dict) -> dict | None:
    """The Firmware stage's reads: `bmc-checked` versions ({component: version} for
    Facebook OpenBMC, report only) + each part's current/target/phase (BMC, BIOS, each
    Mellanox card) for the TiogaPass manifests."""
    r = runs.get("bmc-checked")
    if not r or r.get("status") != "done":
        return None
    d = _detail(r)
    sl = d.get("slice") or {}
    parts = []
    for step, name in (("bmc-checked", "BMC"), ("bios-checked", "BIOS")):
        p = _part(name, _detail(runs.get(step)).get("slice") or {})
        if p:
            parts.append(p)
    for c in (_detail(runs.get("mlx-checked")).get("slice") or {}).get("devices") or []:
        p = _part(f"NIC {c.get('pci')}", c)
        if p:
            parts.append(p)
    bk = runs.get("bmc-backup-checked")     # a TiogaPass's backup chip (read, else none)
    if bk and bk.get("status") == "done" and bk.get("verdict") == "pass" \
            and _detail(bk).get("backup_version"):
        parts.insert(1, {"part": "BMC backup", "current": _detail(bk)["backup_version"],
                         "target": None, "phase": None})
    if sl.get("versions"):                  # Facebook OpenBMC: the versions are the read
        parts = [p for p in parts if p["part"] not in ("BMC", "BMC backup")]
    return {"phase": d.get("phase") or sl.get("phase"), "versions": sl.get("versions") or {},
            "not_present": sl.get("not_present") or {}, "parts": parts,
            "headline": d.get("headline"), "artifacts": r.get("artifacts") or {}}


def endpoint(runs: dict, mac_step: str, lease_step: str) -> dict:
    """{mac, ip} one switch endpoint's Discover steps found (dynamic lease)."""
    return {"mac": _fmt(_detail(runs.get(mac_step)).get("mac")),
            "ip": _detail(runs.get(lease_step)).get("ip")}


def _oper(run):
    """`oper` of a switchportlink read ('up' / 'down' / None when unread)."""
    if not run or run.get("status") != "done":
        return None
    v = _row(_detail(run), "oper")
    return None if v in (None, "unread", "None") else v


def mgmt_ports(g, s, raw: dict) -> list[dict]:
    """The mgmt switch's ports: FDB MACs, the geometry device(s) cabled there, link."""
    fdb = _detail(raw.get(("_rack", "switch-fdb"))).get("fdb") or []
    unexp = {u["port"] for u in _detail(raw.get(("_rack", "slot-match"))).get("unexpected") or []}
    ports: dict[int, dict] = {}

    def p(n):
        return ports.setdefault(n, {"port": str(n), "iface": f"Ethernet{n - 1}", "seen": [],
                                    "devices": [], "oper": None, "note": ""})
    for e in fdb:
        try:
            n = iface_port(e.get("iface") or "")
        except ValueError:
            continue
        m = _fmt(e.get("mac"))
        if m not in p(n)["seen"]:
            p(n)["seen"].append(m)
    for d in g.devices:
        for name, l in sorted(d.links.items()):
            if l.switch != s.id:
                continue
            key = f"{d.pos}.{name}" if name.startswith("iom") else d.pos
            row = p(l.port)
            row["devices"].append({"pos": d.pos, "link": name,
                                   "sel": key if name.startswith("iom") else d.pos})
            row["oper"] = row["oper"] or _oper(raw.get((key, "switchportlink")))
    for n in s.uplink_ports:
        p(n)["note"] = "uplink"
    for n in unexp:
        p(n)["note"] = "unexpected MAC"
    return [ports[n] for n in sorted(ports)]


def tor_ports(g, s, raw: dict) -> list[dict]:
    """A wedge's geometry lanes -> device + the LLDP neighbour on that interface."""
    lldp = _detail(raw.get(("_rack", f"lldp-{s.id}"))).get("lldp") or []
    by_if = {}
    for e in lldp:
        by_if.setdefault(e.get("local_iface"), []).append(
            " ".join(x for x in (e.get("remote_device"), e.get("remote_descr")
                                 or e.get("remote_port")) if x))
    rows, seen = [], set()
    for d in g.devices:
        for name, l in sorted(d.links.items()):
            if l.switch != s.id:
                continue
            ifn = wedge_iface(l.port, l.lane)
            seen.add(ifn)
            rows.append({"port": f"{l.port}" + (f"/{l.lane}" if l.lane else ""), "iface": ifn,
                         "seen": by_if.get(ifn, []),
                         "devices": [{"pos": d.pos, "link": name, "sel": d.pos}],
                         "oper": None, "note": ""})
    for ifn, nb in sorted(by_if.items()):
        if ifn not in seen:
            rows.append({"port": "", "iface": ifn, "seen": nb, "devices": [], "oper": None,
                         "note": "neighbour on a lane no device claims"})
    rows.sort(key=lambda r: int(r["iface"][8:]) if r["iface"][8:].isdigit() else 1 << 30)
    return rows


def switch_view(g, s, runs: dict, raw: dict) -> dict:
    """Everything a switch drawer shows beside its step rows."""
    ep = s.endpoints
    return {"serial": (identity(runs) or {}).get("serial"), "identity": identity(runs),
            "fw": firmware(runs),
            "sonic": endpoint(runs, "host-mac-seen", "host-leased") if ep.get("sonic") else None,
            "bmc": endpoint(runs, "bmc-mac-seen", "bmc-leased") if ep.get("bmc") else None,
            "ports": mgmt_ports(g, s, raw) if s.role == "mgmt" else tor_ports(g, s, raw),
            "ports_from": ("FDB (Discover's switch-fdb) + switchportlink" if s.role == "mgmt"
                           else f"geometry lanes + LLDP (Qualify's lldp-{s.id})")}
