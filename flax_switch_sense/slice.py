"""Slice raw eAPI tables into a per-port dict.

Output schema matches the spec's switch_facts.ports JSON column shape
(except for classification fields, which the publisher adds via classify_macs).
"""


def slice_by_port(
    link: dict[str, str],
    vlans: dict[str, dict],
    macs: list[tuple[int, str, str]],
    lldp: dict[str, list[dict]],
) -> dict[str, dict]:
    """Combine link/vlan/mac/lldp tables into {port_name: {...}}.

    Output keys per port:
      link           : 'link' | 'nolink' | 'unknown'
      mask           : 'access' | 'trunk' (omitted if unknown)
      access_vid     : int (access ports only; omitted on trunks / unknown)
      macs           : [mac_str, ...]
      lldp_neighbors : [{mac, sysname, port_description, mgmt_addrs[]}, ...]
    """
    out: dict[str, dict] = {}
    # Seed with every port that appears in any input table so consumers see it.
    every_port = set(link) | set(vlans) | set(lldp) | {p for _, _, p in macs}
    for port in every_port:
        out[port] = {
            "link": link.get(port, "unknown"),
            "macs": [],
            "lldp_neighbors": lldp.get(port, []),
        }
        v = vlans.get(port)
        if v is not None:
            out[port]["mask"] = "trunk" if v["trunk"] else "access"
            # access_vid is only meaningful for access ports — omit on trunks
            # so flax-classify's feeder can skip them cleanly without sentinel
            # checks.
            if not v["trunk"] and v.get("access_vid") is not None:
                out[port]["access_vid"] = v["access_vid"]

    for _vlan, mac, port in macs:
        out[port]["macs"].append(mac)

    return out
