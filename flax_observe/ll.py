"""IPv6 link-local helpers for reaching OpenBMC before any IPv4 lease.

Primitives are faithful copies of the helpers in flax_reconcile/kick.py,
which were themselves lifted verbatim from scripts/reaper_leased.py.
Kept here (rather than imported from flax_reconcile) so that flax-observe's
Docker image does not need a flax_reconcile dependency.

Python 3.11: no backslashes inside f-string {}.
"""
import ipaddress
import subprocess


def _normalise_mac(mac):
    """Normalise to colon-separated lower hex.  Accepts dash- or dot-separated
    and mixed-case input (mirrors flax_reconcile.kick.normalise_mac)."""
    return mac.replace("-", ":").replace(".", ":").lower()


def eui64_ll_from_mac(mac: str) -> str:
    """Return the IPv6 link-local EUI-64 address for *mac* in canonical
    compressed form (matching ``ip -6 addr`` output).

    The U/L bit (bit 1 of the first octet) is flipped (XOR 0x02), then
    ``ff:fe`` is inserted between the OUI and the NIC-specific bytes.

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


def ping6_reachable(addr: str, timeout: int = 2, runner=None) -> bool:
    """Return True iff a single ping6 packet to *addr* is answered within
    *timeout* seconds.

    *addr* may include a ``%iface`` zone suffix (e.g.
    ``fe80::3e2c:99ff:fe58:9d02%eth0.26``).

    *runner* is an optional ``(addr, timeout) -> bool`` callable injected
    in tests.  When *runner* is None the real ``ping6`` binary is executed
    via subprocess.

    Never raises — any exception (including an exception raised by *runner*)
    is caught and returns False.
    """
    if runner is not None:
        try:
            return bool(runner(addr, timeout))
        except Exception:
            return False
    try:
        r = subprocess.run(
            ["ping6", "-c", "1", "-W", str(int(timeout)), addr],
            timeout=timeout + 1,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def ll_target(mac: str, iface: str) -> str:
    """Return the zoned link-local address ``fe80::…%iface`` used by ssh/ping6."""
    return eui64_ll_from_mac(mac) + "%" + iface
