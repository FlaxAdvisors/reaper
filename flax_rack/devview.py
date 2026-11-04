"""Drawer data for /api/state: identity, firmware versions and switch port tables.

Read-only views over order.db's latest runs (never a device read): every switch, every
BMC endpoint (a TiogaPass BMC, each JBOD IOM, a wedge's BMC) gets its own

  identity  the Discover `serial` step's read: serial + the kind's fields (TiogaPass
            FRU 0, IOM `fruid-util iom`, wedge `weutil`, SONiC `show platform syseeprom`)
  fw        the Firmware reads (the drawer's firmware block; tiles carry none): a
            TiogaPass's BIOS, BMC active, BMC backup chip and each Mellanox port (PSID)
            with current/target/phase; an IOM's BMC + ROM and the rest of its fw-util
            read (report only) + not-present parts

and a switch its mgmt / BMC MAC+IP (Discover's *-mac-seen / *-leased) and a per-port
table: mgmt switch = FDB MACs per port -> the geometry device cabled there + link state
(the device's switchportlink read); a wedge = each geometry pcieN lane -> device +
the LLDP neighbour Qualify read on that interface.
"""
import time
from datetime import datetime

from .discover_ladder import LADDER_BUDGET_S
from .switches import front_panel, link_iface


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


def _done(run):
    return run if run and run.get("status") == "done" else None


def _backup_row(run, label) -> dict | None:
    """The backup chip's row (TiogaPass backup BMC / IOM ROM): its version when the read
    passed, else "not read" with the reason; None when the step is n/a or never ran."""
    r = _done(run)
    if not r or (r.get("verdict") == "skip" and _detail(r).get("na")):
        return None
    v = _detail(r).get("backup_version") if r.get("verdict") == "pass" else None
    return {"part": label, "current": v, "target": None, "phase": None,
            "why": None if v else (_detail(r).get("reason") or _detail(r).get("headline")
                                   or r.get("verdict"))}


def firmware(runs: dict) -> dict | None:
    """The Firmware stage's reads for the drawer's firmware block. `parts`, in order:
    TiogaPass = BIOS, BMC active, BMC backup, NIC per port (current/target/phase, PSID);
    Facebook OpenBMC (an IOM) = BMC + ROM from fw-util; `versions` = the rest of a
    fw-util read (report only); `not_present` its absent components."""
    r = _done(runs.get("bmc-checked"))
    if not r:
        return None
    d = _detail(r)
    sl = d.get("slice") or {}
    vers = dict(sl.get("versions") or {})
    parts = []
    if vers:                                # Facebook OpenBMC: the versions are the read
        bmc = vers.pop("BMC Version", None)
        if bmc:
            parts.append({"part": "BMC", "current": bmc, "target": None, "phase": None})
        rom = _backup_row(runs.get("bmc-backup-checked"), "ROM")
        if rom:
            vers.pop("ROM Version", None)
            parts.append(rom)
    else:
        for step, name in (("bios-checked", "BIOS"), ("bmc-checked", "BMC active")):
            p = _part(name, _detail(runs.get(step)).get("slice") or {})
            if p:
                parts.append(p)
        bk = _backup_row(runs.get("bmc-backup-checked"), "BMC backup")
        if bk:
            parts.append(bk)
    for c in (_detail(runs.get("mlx-checked")).get("slice") or {}).get("devices") or []:
        p = _part(f"NIC {c.get('port') or c.get('pci') or '?'}", c)
        if p:
            p.update(psid=c.get("psid"), target_psid=c.get("target_psid"))
            parts.append(p)
    return {"phase": d.get("phase") or sl.get("phase"), "versions": vers,
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
        return ports.setdefault(n, {"port": str(n), "iface": link_iface(s, n, None), "seen": [],
                                    "devices": [], "oper": None, "note": ""})
    for e in fdb:
        fp = front_panel(s, e.get("iface") or "")
        if fp is None:
            continue
        n = fp[0]
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
            ifn = link_iface(s, l.port, l.lane)
            seen.add(ifn)
            rows.append({"port": f"{l.port}" + (f"/{l.lane}" if l.lane else ""), "iface": ifn,
                         "seen": by_if.get(ifn, []),
                         "devices": [{"pos": d.pos, "link": name, "sel": d.pos}],
                         "oper": None, "note": ""})
    for ifn, nb in sorted(by_if.items()):
        if ifn not in seen:
            rows.append({"port": "", "iface": ifn, "seen": nb, "devices": [], "oper": None,
                         "note": "neighbour on a lane no device claims"})
    def order(r):
        fp = front_panel(s, r["iface"])
        return (fp[0], fp[1] or 0) if fp else (1 << 30, 0)
    rows.sort(key=order)
    return rows


def switch_view(g, s, runs: dict, raw: dict) -> dict:
    """Everything a switch drawer shows beside its step rows."""
    ep = s.endpoints
    return {"serial": (identity(runs) or {}).get("serial"), "identity": identity(runs),
            "fw": firmware(runs),
            # the switch's own mgmt port: SONiC, or a Wedge 400's x86 CPU module (no SONiC yet)
            "sonic": endpoint(runs, "host-mac-seen", "host-leased")
            if ep.get("sonic") or ep.get("cpu") else None,
            "bmc": endpoint(runs, "bmc-mac-seen", "bmc-leased") if ep.get("bmc") else None,
            "ports": mgmt_ports(g, s, raw) if s.role == "mgmt" else tor_ports(g, s, raw),
            "ports_from": ("FDB (Discover's switch-fdb) + switchportlink" if s.role == "mgmt"
                           else f"geometry lanes + LLDP (Qualify's lldp-{s.id})")}


# ---------------------------------------------------------------- Discover timeline
# Post's step-modal ladder (flax_post app.js ladderRows), built from the device's
# Discover rows: power-on (or the kernel boot of a node found up), each boot mark with
# its offset, the skipped / fault rows, and the run's end (Discover is one-shot: its
# last row is "Discover ended", not Post's live "rung now").
LADDER_RUNGS = ("power-on", "tftp-seen", "ipxe-seen", "live-iso-seen", "host-leased",
                "host-pinged", "host-ssh", "bmc-ready")
_MARKS = (("tftp-seen", "TFTP request seen"), ("ipxe-seen", "iPXE script fetched"),
          ("live-iso-seen", "live ISO fetched"), ("host-pinged", "host answered ping"),
          ("host-ssh", "host answered ssh"), ("bmc-ready", "BMC answered a data read"))
# steps that record the moment they answered as they finish: a row from before they
# stored seen_at (2026-11-04) shows its finish time, with no offset
_AT_FINISH = ("host-pinged", "host-ssh", "bmc-ready")
TIMELINE_COLS = ["time", "since on", "event", ""]


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except (TypeError, ValueError):
        return None


def _clock(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""


def _span(s):
    s = max(0, int(round(s)))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def discover_timeline(runs: dict) -> dict | None:
    """The device's Discover boot timeline: {anchor, anchor_kind, rows: [[time, since,
    event, extra]], row_cls, events: {rung: epoch}}. Anchor = power-on, else the kernel
    boot of a node already up (live image / installed OS), else None (absolute times,
    blank offsets). None for a ladder with no host (an IOM roll-up) or before Discover."""
    disc = {k: v for k, v in runs.items() if (v or {}).get("stage") == "discover"}
    po = disc.get("power-on")
    if not po or (po.get("verdict") == "skip" and _detail(po).get("na")):
        return None
    pd = _detail(po)
    anchor, kind = _num(pd.get("power_on_at")), "power-on"
    if anchor is None:
        anchor, kind = _num(pd.get("kernel_boot")), "kernel boot"
    if anchor is None:
        kind = None

    def since(x):           # a mark before a kernel-boot anchor (the live boot) is negative
        if x is None or anchor is None:
            return ""
        return ("-" if x < anchor - 0.5 else "+") + _span(abs(x - anchor))

    head, marks, tail, events = [], [], [], {}
    if kind == "power-on":
        head.append(([_clock(anchor), "+0s", "power-on", "by the engine"
                      + (f" ({pd['power_action']})" if pd.get("power_action") else "")], ""))
        events["power-on"] = anchor
    elif kind == "kernel boot":
        where = "installed OS" if pd.get("installed_os") else "live image"
        head.append(([_clock(anchor), "+0s", "power-on",
                      f"already on: kernel booted ({where}), no power action"], ""))
        events["power-on"] = anchor
    skipped = None
    for rung, name in _MARKS:
        r = disc.get(rung)
        if not r or r.get("status") != "done":
            continue
        d = _detail(r)
        if r.get("verdict") == "skip" and d.get("na") and rung in ("tftp-seen", "ipxe-seen",
                                                                   "live-iso-seen"):
            skipped = skipped or d.get("reason") or d.get("headline")
            continue
        if r.get("verdict") != "pass":
            continue
        at = _num(d.get("seen_at"))
        if at is not None:
            events[rung] = at
            marks.append((at, [_clock(at), since(at), name, ""]))
        elif rung in _AT_FINISH and _epoch(r.get("finished_at")):
            marks.append((_epoch(r["finished_at"]), [_clock(_epoch(r["finished_at"])), "", name,
                                                      "row finish time"]))
    marks.sort(key=lambda m: m[0])
    if skipped:
        tail.append((["", "", "boot markers not observed", skipped], ""))
    done = [v for v in disc.values() if v.get("status") == "done"]
    rung, fault = next(((k, v) for k, v in sorted(disc.items(),
                                                   key=lambda kv: kv[1].get("id") or 0)
                        if v.get("verdict") in ("fail", "unmeasured")), (None, None))
    if fault:
        at = _epoch(fault.get("finished_at"))
        fd = _detail(fault)
        tail.append(([_clock(at), since(at) if at else "", "✕ " + rung,
                      fd.get("error") or fd.get("headline") or fault.get("verdict")], "fault"))
    running = [k for k, v in disc.items() if v.get("status") == "running"]
    if running:
        at = _epoch(disc[running[0]].get("started_at"))
        tail.append(([_clock(at), since(at) if at else "", f"rung now: {running[0]}", ""], "now"))
    elif done:
        vs = {v.get("verdict") for v in done}
        verdict = "fail" if "fail" in vs else "unmeasured" if "unmeasured" in vs else "pass"
        at = max((_epoch(v.get("finished_at")) or 0) for v in done) or None
        tail.append(([_clock(at), since(at) if at else "",
                      f"Discover ended: {verdict} at {_clock(at)}", ""], "now"))
    rows = head + [(m[1], "") for m in marks] + tail
    return {"anchor": anchor, "anchor_kind": kind, "rows": [r for r, _ in rows],
            "row_cls": [c for _, c in rows], "events": events}


def _set_row(rows, label, value):
    for r in rows:
        if r and r[0] == label:
            r[1] = value
            return
    rows.append([label, value])


def attach_timeline(runs: dict) -> None:
    """Every Discover ladder rung of a device gains Post's modal pieces: `after power-on`
    (after kernel boot for a node found up), `budget`, and the shared timeline table."""
    tl = discover_timeline(runs)
    if not tl:
        return
    label = "after kernel boot" if tl["anchor_kind"] == "kernel boot" else "after power-on"
    table = {"cols": TIMELINE_COLS, "rows": tl["rows"], "row_cls": tl["row_cls"],
             "cls": "timeline"}
    for rung in LADDER_RUNGS:
        r = runs.get(rung)
        d = (r or {}).get("detail")
        if not r or r.get("stage") != "discover":
            continue
        if not isinstance(d, dict):
            d = r["detail"] = {}
        rows = d.setdefault("rows", [])
        at = tl["events"].get(rung)
        if at is not None and tl["anchor"] is not None:
            _set_row(rows, label, ("-" if at < tl["anchor"] - 0.5 else "+")
                     + _span(abs(at - tl["anchor"])))
        if rung in LADDER_BUDGET_S:
            _set_row(rows, "budget", f"{LADDER_BUDGET_S[rung]} s")
        d["table"] = table
