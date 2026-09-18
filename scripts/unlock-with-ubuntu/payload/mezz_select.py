#!/usr/bin/env python3
# scripts/unlock-with-ubuntu/payload/mezz_select.py
"""Pick which Mellanox cards the unlock station may flash.

The station may carry a PCIe ConnectX uplink alongside the jumpered mezzanine
card it exists to flash. Burning the uplink would kill the operator's own
session mid-flash, so selection is by EXCLUSION from three independent sources:

  1. any card whose netdev holds an IPv4 address
  2. the card backing the default route
  3. explicit PROTECT_PCI entries in /etc/flax/mezz-flash.conf

A card is protected as a WHOLE -- both functions of a dual-port card share one
flash image -- so protection is matched at bus:device ("19:00"), never at the
function level.

Deliberately NOT positive selection by a pinned mezzanine BDF: that fails
silently on an unexpected board (nothing selected, station blinks 'done',
nothing flashed), and the station has no way to signal failure at the rack.
Exclusion plus a logged target list is the safer trade.
"""


def card(bdf):
    """'19:00.1' -> '19:00'. The flash unit is the card, not the function."""
    return bdf.rsplit(".", 1)[0]


def select(devices, ifaces, protect=()):
    """-> {"targets": [...], "protected": [...]}, both sorted.

    devices: every 15b3: BDF seen by lspci, e.g. ["06:00.0", "19:00.0", "19:00.1"]
    ifaces:  [{"pci": bdf, "ipv4": bool, "default_route": bool}, ...]
    protect: config entries, each a BDF or a bare bus:device
    """
    blocked = {card(p) for p in protect}
    for i in ifaces:
        if i.get("ipv4") or i.get("default_route"):
            blocked.add(card(i["pci"]))
    targets = [d for d in devices
               if d.endswith(".0") and card(d) not in blocked]
    return {"targets": sorted(targets), "protected": sorted(blocked)}


# --- system gathering (thin; the logic above is what the tests cover) -------

CONF = "/etc/flax/mezz-flash.conf"


def _sh(cmd):
    import subprocess
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=30).stdout
    except Exception:
        return ""


def _devices():
    return [ln.split()[0] for ln in _sh("lspci -d 15b3:").splitlines() if ln.strip()]


def _ifaces():
    import os
    default = _sh("ip -o route show default")
    out = []
    for name in sorted(os.listdir("/sys/class/net")):
        link = "/sys/class/net/%s/device" % name
        if not os.path.exists(link):
            continue
        # .../0000:19:00.1 -> 19:00.1 (mstflint addresses cards without the domain)
        bdf = os.path.basename(os.path.realpath(link))
        if bdf.count(":") == 2:
            bdf = bdf.split(":", 1)[1]
        out.append({
            "name": name,
            "pci": bdf,
            "ipv4": bool(_sh("ip -o -4 addr show dev %s" % name).strip()),
            "default_route": (" dev %s " % name) in default,
        })
    return out


def _protect():
    import os
    if not os.path.exists(CONF):
        return []
    vals = []
    with open(CONF) as f:
        for line in f:
            line = line.strip()
            if line.startswith("PROTECT_PCI="):
                vals += line.split("=", 1)[1].strip().strip('"').split()
    return vals


if __name__ == "__main__":
    import sys
    got = select(_devices(), _ifaces(), _protect())
    if got["protected"]:
        sys.stderr.write("protected (addressed / configured): %s\n"
                         % " ".join(got["protected"]))
    for t in got["targets"]:
        print(t)
