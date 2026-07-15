"""IPv6 link-local (EUI-64) helpers for the flax-control device page.

The EUI-64 primitive is lifted verbatim-faithful from flax_observe/ll.py
(itself a copy of flax_reconcile/kick.py, originally scripts/reaper_leased.py).
Kept here so flax-control's Docker image does not need a flax_observe or
flax_reconcile dependency just to render a link-local address.

Python 3.11: no backslashes inside an f-string {}.
"""
import ipaddress


def _normalise_mac(mac: str) -> str:
    """Normalise to colon-separated lower hex. Accepts dash- or dot-separated
    and mixed-case input (mirrors flax_observe.ll._normalise_mac)."""
    return mac.replace("-", ":").replace(".", ":").lower()


def eui64_ll_from_mac(mac: str) -> str:
    """Return the IPv6 link-local EUI-64 address for *mac* in canonical
    compressed form (matching ``ip -6 addr`` output).

    The U/L bit (bit 1 of the first octet) is flipped (XOR 0x02), then
    ``ff:fe`` is inserted between the OUI and the NIC-specific bytes.
    Known pair: 1c:34:da:7f:9d:32 -> fe80::1e34:daff:fe7f:9d32.

    Raises ValueError for a mac that does not split into exactly six octets.
    """
    parts = _normalise_mac(mac).split(":")
    if len(parts) != 6:
        raise ValueError("bad mac: " + repr(mac))
    first = int(parts[0], 16) ^ 0x02
    octets = [first.to_bytes(1, "big").hex()] + parts[1:3] + ["ff", "fe"] + parts[3:6]
    joined = "".join(octets)
    full = "fe80::" + ":".join(joined[i:i + 4] for i in range(0, 16, 4))
    return str(ipaddress.IPv6Address(full))


def ll_with_zone(mac: str, vid, parent: str = "eth0") -> str:
    """Return the zoned link-local ``fe80::EUI64%<parent>.<vid>``.

    *parent* is the bang host's mgmt-side parent interface (eth0 on
    braintree/eindhoven); the VLAN sub-interface ``<parent>.<vid>`` is the
    zone an operator would use to ssh/ping6 the device on its access VLAN.
    When *vid* is falsy the bare parent is used (no sub-interface suffix).
    """
    ll = eui64_ll_from_mac(mac)
    zone = parent if not vid else parent + "." + str(vid)
    return ll + "%" + zone
