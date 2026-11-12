"""MAC address helpers shared across flax-switch-sense.

Lifted from scripts/switchportrecond.py (which itself lifted from
reaper_leased.py). Behaviour kept byte-identical so that classify_macs
returns the same results in either home.
"""


def normalize_mac(s: str) -> str:
    """Return lowercase colon-separated MAC, accepting :, -, or . separators."""
    digits = "".join(c for c in s.lower() if c in "0123456789abcdef")
    if len(digits) != 12:
        raise ValueError(f"bad MAC string: {s!r}")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def mac_to_int(mac: str) -> int:
    return int(mac.replace(":", ""), 16)


def int_to_mac(n: int) -> str:
    h = format(n & 0xFFFFFFFFFFFF, "012x")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def _paired_nic_offsets() -> list[int]:
    """Stride for paired NIC pattern. BMC - 2k for k in 1..4 covers
    leopard/tiogapass (k=1) and the multi-blade sameport families
    (brycecanyon and similar; k=1..4)."""
    return [2, 4, 6, 8]


def is_paired_nic_of(nic: str, bmc: str) -> bool:
    """True iff nic in {bmc-2, bmc-4, bmc-6, bmc-8} (paired-NIC pattern)."""
    try:
        diff = mac_to_int(bmc) - mac_to_int(nic)
    except ValueError:
        return False
    return diff in _paired_nic_offsets()
