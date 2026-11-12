#!/usr/bin/env python3
# scripts/unlock-with-ubuntu/payload/mezz_select.py
"""Pick which Mellanox cards the unlock station may flash.

Selection is by EXCLUSION, and PROTECT_PCI is the ONLY thing that excludes.

A card is protected as a WHOLE -- both functions of a dual-port card share one
flash image -- so protection is matched at bus:device ("19:00"), never at the
function level.

Deliberately NOT positive selection by a pinned mezzanine BDF: that fails
silently on an unexpected board (nothing selected, station blinks 'done',
nothing flashed), and the station has no way to signal failure at the rack.
Exclusion plus a logged target list is the safer trade.

HISTORY (2026-09-21, operator decision): this also used to exclude any card
holding an IPv4 address or backing the default route, to avoid burning the
card carrying the operator's own ssh session mid-flash. That is REMOVED. We
routinely flash NICs on PXE-live-booted machines and the link returns after the
FW update and reset, so the caution bought nothing -- and it cost real
coverage, because a failed-unlock card that came back up with a lease was
silently skipped. That is precisely the card that most needs another pass:
it may carry a new PSID and still be locked, or still need its UEFI ROM.

Addressed cards are still REPORTED, because flashing the card under your own
session is worth knowing about. They are simply no longer refused.
"""


def card(bdf):
    """'19:00.1' -> '19:00'. The flash unit is the card, not the function."""
    return bdf.rsplit(".", 1)[0]


def select(devices, ifaces, protect=()):
    """-> {"targets": [...], "protected": [...], "addressed": [...]}, all sorted.

    devices: every 15b3: BDF seen by lspci, e.g. ["06:00.0", "19:00.0", "19:00.1"]
    ifaces:  [{"pci": bdf, "ipv4": bool, "default_route": bool}, ...]
    protect: config entries, each a BDF or a bare bus:device

    `ifaces` no longer gates anything -- it only populates "addressed", which
    the caller logs so the operator can see it is about to flash a card that
    currently holds a lease.
    """
    blocked = {card(p) for p in protect}
    addressed = {card(i["pci"]) for i in ifaces
                 if i.get("ipv4") or i.get("default_route")}
    targets = [d for d in devices
               if d.endswith(".0") and card(d) not in blocked]
    return {"targets": sorted(targets),
            "protected": sorted(blocked),
            "addressed": sorted(addressed)}


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
        sys.stderr.write("protected (PROTECT_PCI): %s\n"
                         % " ".join(got["protected"]))
    if got["addressed"]:
        sys.stderr.write("NOTE: these cards hold an address and will still be "
                         "flashed: %s\n" % " ".join(got["addressed"]))
    for t in got["targets"]:
        print(t)
