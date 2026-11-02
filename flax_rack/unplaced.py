"""Lab racks: the hardware on the rack switch that no BOM slot holds yet ("Unplaced").

Discover lists every (port, lane) of the rack switch that carries MACs no placement
claims (uplinks aside). The pair on a port is told apart by asking each MAC's leased IP
for its FRU 0 over IPMI: the one that answers is the BMC, the other the host -- never
an offset (howlin's Tioga Pass on Et18/1 is host+3). A port whose MACs give no FRU
answer is listed with what is known and cannot be placed until it answers. Nothing here
places anything: only the operator's Place does (no inferred placement, ever).
The serial is the BMC FRU's family field (Leopard: Chassis Serial), never the host's
DMI serial ("To be filled by O.E.M." on every howlin host)."""
from flax_post.observe import fru, family_map_live


def fru_summary(text: str, family_map) -> dict | None:
    """{product, serial, family} from `ipmitool fru print 0` text (Post's family-aware
    reading), None when FRU 0 is absent."""
    bb = fru.read_baseboard(text, family_map)
    if bb["state"] == "absent":
        return None
    return {"product": bb["product_name"] or bb["board_product"] or "",
            "serial": bb["serial"] or "", "family": bb["family"] or ""}


def fru_prober(tp_fru, creds, *, family_map=None, runner=None):
    """ip -> fru_summary of that BMC (or None): tp_fru tries every BMC credential x cipher
    (17, then 3: Leopard answers on 3); its raw text is read even when the TiogaPass
    Product Serial rule failed it (a Leopard has none)."""
    def probe(ip):
        kw = {"runner": runner} if runner else {}
        r = tp_fru(ip, creds, **kw)
        text = (r.artifacts or {}).get("fru0.txt")
        if not text:
            return None
        return fru_summary(text, family_map if family_map is not None else family_map_live.current())
    return probe


def find(by_port, placements, leases, fru_probe, uplinks, switch_id="H") -> list[dict]:
    """The unplaced hardware, one entry per (port, lane), port order.
    by_port {(port, lane): [mac]}, placements {pos: {switch, port, lane, ...}},
    leases {mac: ip} (active), fru_probe(ip) -> {product, serial, family} | None."""
    claimed = {(int(p["port"]), p.get("lane")) for p in placements.values()
               if p.get("switch") == switch_id}
    out = []
    for key in sorted(by_port, key=lambda k: (k[0], k[1] or 0)):
        if key in claimed or key[0] in uplinks:
            continue
        macs = list(by_port[key])
        bmc, info = None, None
        for m in macs:
            ip = leases.get(m)
            info = fru_probe(ip) if ip else None
            if info:
                bmc = m
                break
        others = [m for m in macs if m != bmc]
        host = others[0] if len(others) == 1 else None
        out.append({"switch": switch_id, "port": key[0], "lane": key[1],
                    "host_mac": host, "bmc_mac": bmc,
                    "host_ip": leases.get(host) if host else None,
                    "bmc_ip": leases.get(bmc) if bmc else None, "macs": macs,
                    **(info or {"product": "", "serial": "", "family": ""})})
    return out
