"""HA-path check: each host's pcieN NIC ports must show up in LLDP on the
wedge and breakout lane the geometry cables them to."""
import re

from .result import RungResult
from .sonic import LldpEntry, wedge_iface


def _mac(s: str) -> str:
    return re.sub(r"[^0-9a-f]", "", s.lower())


def _is_node(e: LldpEntry, hostnames: set[str], macs: set[str]) -> bool:
    # MAC first (hostname may be "localhost"), hostname second
    return _mac(e.remote_port) in macs or e.remote_device.lower() in hostnames


def _hostnames(g, d) -> set[str]:
    base = f"{g.prefix}{d.pos.lower()}"
    return {base} | {f"{base}-n{k}" for k in range(1, 10)}


def check(g, lldp_by_switch: dict[str, list[LldpEntry]],
          node_macs: dict[str, dict[str, str]]) -> dict[str, RungResult]:
    """One RungResult per host-bearing pos; detail["links"] lists, per pcieN link,
    {"link", "expected", "seen", "ifname"} (seen = "<switch> <iface>" or None)."""
    out = {}
    for d in g.devices:
        if not d.endpoints.get("hosts"):
            continue
        links = {n: l for n, l in sorted(d.links.items()) if n.startswith("pcie")}
        if not links:
            continue
        ifmac = {i: _mac(m) for i, m in (node_macs.get(d.pos) or {}).items()}
        mac2if = {m: i for i, m in ifmac.items()}
        hn = {h.lower() for h in _hostnames(g, d)}
        seen = [(sw, e) for sw, es in lldp_by_switch.items() for e in es
                if _is_node(e, hn, set(ifmac.values()))]
        claimed = set()
        rows = {}
        # exact matches claim their entry first so a leftover can be named as "seen"
        for name, l in links.items():
            want = wedge_iface(l.port, l.lane)
            for i, (sw, e) in enumerate(seen):
                if i not in claimed and sw == l.switch and e.local_iface == want:
                    claimed.add(i)
                    rows[name] = i
                    break
        problems = []
        details = []
        for name, l in links.items():
            want = wedge_iface(l.port, l.lane)
            exp = f"{l.switch} {want} ({l.port}" + (f"/{l.lane})" if l.lane else ")")
            i = rows.get(name)
            if i is None:
                rest = [k for k in range(len(seen)) if k not in claimed]
                i = rest[0] if rest else None
                if i is not None:
                    claimed.add(i)
            if i is None:
                details.append({"link": name, "expected": exp, "seen": None,
                                "ifname": None})
                problems.append(f"{name}: expected {exp}, not seen on any wedge")
                continue
            sw, e = seen[i]
            ifname = mac2if.get(_mac(e.remote_port)) or e.remote_descr or None
            details.append({"link": name, "expected": exp,
                            "seen": f"{sw} {e.local_iface}", "ifname": ifname})
            if rows.get(name) is None:
                problems.append(f"{name}: expected {exp}, seen {sw} {e.local_iface}")
        out[d.pos] = RungResult("fail" if problems else "pass",
                                {"links": details, **({"error": "; ".join(problems)}
                                                      if problems else {})})
    return out
