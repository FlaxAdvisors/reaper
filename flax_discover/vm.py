"""MAC normalisation + VM heuristic.

Lifted VERBATIM from ``scripts/reaper_leased.py`` (section 4. mac_pair).
``normalise_mac`` and ``is_vm_mac`` must stay byte-identical to the legacy
enroller so the new pipeline detects VMs identically to the legacy daemon.
"""


def normalise_mac(mac):
    """Normalise to colon-lower hex. Accepts dashes (Windows-style), mixed case."""
    return mac.replace("-", ":").replace(".", ":").lower()


def is_vm_mac(mac):
    """True iff this MAC is likely a VM (no physical BMC). Heuristics:
    - VirtualBox OUI 08:00:27:* (KVM/qemu sometimes uses 52:54:00).
    - Locally-administered (LAA) bit set in the first byte (containers,
      macvlan, libvirt-generated MACs all set this).
    """
    m = normalise_mac(mac)
    if m.startswith(("08:00:27:", "52:54:00:")):
        return True
    try:
        first = int(m.split(":", 1)[0], 16)
    except ValueError:
        return False
    return bool(first & 0x02)  # LAA bit (second-least-significant bit)
