#!/usr/bin/env python3
# scripts/unlock-with-ubuntu/payload/station_ident.py
"""Who this STATION BLADE is -- read in band, with no network.

Prints the `ident` block of the station's status JSON to stdout. Exits 1 when
the blade cannot name itself, which the caller treats as fatal: a station that
cannot be identified must not run (operator decision 2026-09-22 -- fault the
slot and build another flash server).

WHY THE BLADE AND NOT THE CARD. The BMC's MAC comes out of the mezzanine
card's own MAC block, so it changes with every card swap -- and swapping cards
is this station's whole job. The blade's FRU ID 0 serial does not.

WHY IN BAND. With a jumpered card fitted the mezzanine passes no traffic at
all, and it is the only NIC, so the BMC's NC-SI path is dark too. KCS needs no
network, no cipher suite and no credentials -- unlike LAN IPMI, where Leopard
needs -C 3 and TiogaPass -C 17.

The FRU parsing is NOT done here: flaxfru/ is a verbatim copy of the fleet's
own parser, so a station identifies a blade exactly as flax does.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flaxfru.family_map import load_family_map_dir      # noqa: E402
from flaxfru.fru import read_baseboard                  # noqa: E402

FAMILY_MAP_DIR = os.environ.get("MEZZ_FAMILY_MAP",
                                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "family-map"))
# Channel 1 on every blade type we install. Only relevant in band, where the
# channel is the sole variable -- no cipher, no credentials.
LAN_CHANNEL = os.environ.get("MEZZ_LAN_CHANNEL", "1")

_MAC_RE = re.compile(r"^MAC Address\s*:\s*(\S+)", re.M)
# Anchored and with the colon required, so "IP Address Source : DHCP Address"
# -- which sits directly above it -- cannot match.
_IP_RE = re.compile(r"^IP Address\s+:\s*(\S+)", re.M)


def parse_lan_print(text):
    """`ipmitool lan print 1` -> {"bmc_mac": ..., "bmc_ip": ...}."""
    text = text or ""
    mac = _MAC_RE.search(text)
    ip = _IP_RE.search(text)
    return {
        "bmc_mac": (mac.group(1).strip().lower() if mac else ""),
        "bmc_ip": (ip.group(1).strip() if ip else ""),
    }


def build_ident(fru_text, lan_text, family_map, hostname, host_mac):
    """-> the ident block. `state` is "ok" or "no_serial"."""
    base = read_baseboard(fru_text or "", family_map) or {}
    lan = parse_lan_print(lan_text)
    return {
        "station_sn": base.get("serial") or "",
        "serial_field": base.get("serial_field") or "",
        "family": base.get("family") or "",
        "state": base.get("state") or "no_serial",
        "bmc_mac": lan["bmc_mac"],
        "bmc_ip": lan["bmc_ip"],
        # The OBSERVED card MAC. Never an identity -- it is evidence, for
        # exactly the blades where the fleet's bmc = nic + k formula fails.
        "host_mac": (host_mac or "").strip().lower(),
        "host": hostname or "",
    }


def _run(cmd):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=30)
        return p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def _default_route_mac():
    """MAC of the interface holding the default route, or "".

    Only meaningful when the station is online -- which is exactly when it can
    push. Offline (jumpered) it is empty, and that is correct, not an error.
    """
    route = _run(["ip", "-o", "route", "show", "default"])
    m = re.search(r"\bdev\s+(\S+)", route or "")
    if not m:
        return ""
    link = _run(["ip", "-o", "link", "show", m.group(1)])
    m2 = re.search(r"link/ether\s+(\S+)", link or "")
    return m2.group(1) if m2 else ""


def main():
    # A missing or empty family map must NOT silently become the
    # DEFAULT_SERIAL_FIELD ("Product Serial") fallback in flaxfru/fru.py:
    # match_family({}, ...) always returns None, so on a platform that
    # carries BOTH Chassis Serial and Product Serial this would report a
    # different serial than the fleet with state == "ok" -- the exact
    # cross-field fallback operator ruling 2026-09-14 forbids. Fault closed
    # instead (family-map/ is build-generated and not committed, so this is
    # hit every time this runs from a bare checkout).
    if not os.path.isdir(FAMILY_MAP_DIR):
        sys.stderr.write(
            "station_ident: no family map at %s -- cannot pick a serial "
            "field without one. Refusing to guess.\n" % FAMILY_MAP_DIR)
        return 1
    fm = load_family_map_dir(FAMILY_MAP_DIR)
    if not fm:
        sys.stderr.write(
            "station_ident: family map at %s loaded no families -- cannot "
            "pick a serial field without one. Refusing to guess.\n"
            % FAMILY_MAP_DIR)
        return 1
    ident = build_ident(_run(["ipmitool", "fru"]),
                        _run(["ipmitool", "lan", "print", LAN_CHANNEL]),
                        fm,
                        os.uname()[1],
                        _default_route_mac())
    json.dump(ident, sys.stdout)
    sys.stdout.write("\n")
    if ident["state"] != "ok":
        sys.stderr.write(
            "station_ident: no_serial -- FRU 0 has no %s for family %r.\n"
            "This blade cannot be a flash station.\n"
            % (ident["serial_field"] or "ship serial", ident["family"] or "unknown"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
