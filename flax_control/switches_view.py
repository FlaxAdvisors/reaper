"""Pure augmentation of ports_for_switch() rows with access-VLAN visibility:
the LIVE access vid observed by switch_sense (switch_facts.ports.access_vid)
versus the DESIRED vid classify computed (desired_port.desired_vid). Both are
Arista-canonical on the /switches/{switch} page, so keys join directly."""


def build_port_rows(ports, desired_map):
    out = []
    for p in ports:
        live = p.get("access_vid")
        desired = desired_map.get(p.get("port"))
        mask = p.get("mask")
        if mask == "trunk":
            state = "trunk"
        elif mask != "access":
            state = "none"
        elif live is not None and desired is not None:
            state = "match" if live == desired else "drift"
        elif desired is not None:
            state = "desired-only"
        elif live is not None:
            state = "live-only"
        else:
            state = "none"
        out.append({**p, "access_vid_live": live,
                    "access_vid_desired": desired, "vlan_state": state})
    return out
