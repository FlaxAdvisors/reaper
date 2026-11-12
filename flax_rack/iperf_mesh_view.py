"""Qualify's iperf-mesh views (rackpost only): a per-node table on each client's rung and
a rack view on the iperf server's rung, built from flax_rungs.iperf.mesh's results.

Presentation only: verdicts stay mesh's (TX and RX both >= THRESHOLD x the client NIC's
ethtool speed). Deploy's deploy-iperf has its own renderer (operator 2026-11-04); this
module is not shared with it.

The port a network rides: the port LLDP saw for it in the same pass (the ha-path row's
links[].seen), else the planned geometry link; a mismatch reads "planned A:5/1, seen A:6/1".
"""
from flax_rungs.iperf import NETS, THRESHOLD

from .switches import front_panel

HEAD_ORDER = ("A", "B", "bond", "mgmt")        # the headline leads with the fabric
CLIENT_COLS = ["network", "port", "link", "TX Gb/s", "RX Gb/s", "% of line", "judge"]
SERVER_COLS = ["client", *CLIENT_COLS]
NOTE = (f"judge: ok when TX and RX are both >= {THRESHOLD} x the client NIC's link speed "
        "(ethtool); % of line = the lower of TX / RX against that speed")


# ---------------------------------------------------------------- ports
def _fmt_link(sw: str, port: int, lane: int | None) -> str:
    return f"{sw}:{port}" + (f"/{lane}" if lane else "")


def _planned(g, d) -> dict[str, str]:
    """net -> the geometry link it is cabled to: A/B = the pcieN link to that wedge,
    mgmt = the link to the mgmt switch."""
    roles = {s.id: s.role for s in g.switches}
    out = {}
    for name, l in sorted(d.links.items()):
        if name.startswith("pcie") and l.switch in ("A", "B"):
            out.setdefault(l.switch, _fmt_link(l.switch, l.port, l.lane))
        elif roles.get(l.switch) == "mgmt":
            out.setdefault("mgmt", _fmt_link(l.switch, l.port, l.lane))
    return out


def _seen(g, ha_detail: dict | None) -> dict[str, str]:
    """net -> the port the ha-path row's LLDP saw that network's interface on."""
    sws = {s.id: s for s in g.switches}
    out = {}
    for l in (ha_detail or {}).get("links") or []:
        seen = (l or {}).get("seen")
        if not seen or not l.get("ifname"):
            continue
        sw, _, iface = seen.partition(" ")
        fp = front_panel(sws[sw], iface) if sw in sws else None
        out.setdefault(sw, _fmt_link(sw, *fp) if fp else seen)
    return out


def ports_of(g, d, ha_detail: dict | None) -> dict[str, str]:
    """net -> the port column for device `d` (ha_detail: this pass's ha-path row, or None)."""
    planned, seen = _planned(g, d), _seen(g, ha_detail)
    out = {}
    for net in sorted(set(planned) | set(seen)):
        p, s = planned.get(net), seen.get(net)
        if p and s and p != s:
            out[net] = f"planned {p}, seen {s}"
        else:
            out[net] = s or p
    if "A" in out and "B" in out:
        out["bond"] = f"{out['A']} + {out['B']}"
    return out


# ---------------------------------------------------------------- numbers
def _gb(bps) -> str:
    if bps is None:
        return "—"
    v = bps / 1e9
    return f"{v:.1f}" if v >= 10 else f"{v:.2f}"


def _speed(bps) -> str:
    return f"{bps / 1e9:g}G" if bps else "?"


def _pct(n: dict) -> float | None:
    tx, rx, sp = n.get("tx_bps"), n.get("rx_bps"), n.get("speed_bps")
    if tx is None or rx is None or not sp:
        return None
    return 100 * min(tx, rx) / sp


def _pct_s(p: float, ok: bool) -> str:
    s = f"{p:.0f}%"
    if not ok and round(p) >= THRESHOLD * 100:    # never round a fail up to the line
        s = f"{p:.1f}%"
    return s


def _judge(n: dict) -> str:
    if n.get("tx_bps") is None or n.get("rx_bps") is None:
        return "no result"
    if not n.get("speed_bps"):
        return "no link speed"
    return "ok" if n.get("ok") else f"below {THRESHOLD * 100:.0f}%"


def _nets(detail: dict) -> list[str]:
    return [n for n in NETS if isinstance(detail.get(n), dict)]


def _row(net: str, n: dict, port: str | None) -> tuple[list[str], str]:
    p = _pct(n)
    judge = _judge(n)
    return ([net, port or "—", _speed(n.get("speed_bps")), _gb(n.get("tx_bps")),
             _gb(n.get("rx_bps")), _pct_s(p, bool(n.get("ok"))) if p is not None else "—",
             judge], "" if judge == "ok" else "bad")


def _min_txt(pcts: list[tuple[float, bool]]) -> str:
    """pcts: (% of line, ok) per measured path."""
    return f"min {_pct_s(*min(pcts))} of line" if pcts else "no result"


# ---------------------------------------------------------------- views
def client_view(detail: dict, ports: dict[str, str]) -> dict:
    """A client's iperf-mesh detail + table (one row per network, NETS order), headline,
    notes. A detail with no network (server not ready, ...) is returned unchanged."""
    nets = _nets(detail)
    if not nets:
        return dict(detail)
    rows, cls = [], []
    for net in nets:
        r, c = _row(net, detail[net], ports.get(net))
        rows.append(r)
        cls.append(c)
    parts, last_num = [], -1
    for net in (n for n in HEAD_ORDER if n in nets):
        n = detail[net]
        if n.get("tx_bps") is None or n.get("rx_bps") is None:
            parts.append(f"{net} no result")
        else:
            parts.append(f"{net} {_gb(n['tx_bps'])}/{_gb(n['rx_bps'])}")
            last_num = len(parts) - 1
    if last_num >= 0:
        parts[last_num] += " Gb/s"
    pcts = [(p, bool(detail[n].get("ok"))) for n in nets
            if (p := _pct(detail[n])) is not None]
    headline = " · ".join(parts + [_min_txt(pcts)])
    return {**detail, "headline": headline, "notes": [NOTE],
            "table": {"cols": list(CLIENT_COLS), "rows": rows, "row_cls": cls}}


def server_view(server_detail: dict, clients: dict[str, dict], ports_by_pos: dict,
                order: list[str] | None = None) -> dict:
    """The iperf server's detail + a table across the clients of the same run (`order`:
    rack order; default the served list, then any other client), headline
    "served N nodes · M paths · min P% of line" (+ " · worst: <pos> <net> P%" when a path
    is below the line). A client with no network result keeps one "no result" row."""
    served = list(server_detail.get("served") or [])
    seq = [p for p in (order or served) if p in clients]
    seq += [p for p in clients if p not in seq]
    rows, cls, worst, pcts, paths, missing = [], [], None, [], 0, 0
    for pos in seq:
        det = clients[pos] or {}
        nets = _nets(det)
        if not nets:
            why = det.get("headline") or det.get("error") or "no iperf result"
            rows.append([pos, "—", "—", "—", "—", "—", "—", f"no result: {why}"])
            cls.append("bad")
            missing += 1
            continue
        for net in nets:
            r, c = _row(net, det[net], (ports_by_pos.get(pos) or {}).get(net))
            rows.append([pos, *r])
            cls.append(c)
            paths += 1
            p = _pct(det[net])
            if p is None:
                missing += 1
                continue
            pcts.append((p, bool(det[net].get("ok"))))
            if not det[net].get("ok") and (worst is None or p < worst[2]):
                worst = (pos, net, p)
    head = [f"served {len(seq)} node{'s' * (len(seq) != 1)}",
            f"{paths} path{'s' * (paths != 1)}", _min_txt(pcts)]
    if missing:
        head.append(f"{missing} no result")
    if worst:
        head.append(f"worst: {worst[0]} {worst[1]} {_pct_s(worst[2], False)}")
    return {**server_detail, "headline": " · ".join(head), "notes": [NOTE],
            "table": {"cols": list(SERVER_COLS), "rows": rows, "row_cls": cls}}
