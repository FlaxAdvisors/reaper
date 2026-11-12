"""Per-vid MAC-math configs + classifiers.

A MAC-math config describes a hardware family's BMC<->server(s) MAC
relationship, replacing the hardcoded ``bmc = nic +/- 2`` pairing baked
into classify.py. Each config lives at ``/etc/flax/macmath/<vid>.json``
and selects a ``scheme``:

  * ``offset`` -- BMC and host NIC(s) sit at fixed integer offsets from
    the lowest observed MAC on the port (the family's MAC block base).
  * ``distinct_oui`` -- the BMC is the highest observed MAC; the host
    NIC(s) are the macs whose OUI differs from the BMC's (e.g. a SONiC
    mgmt NIC behind a Wedge BMC).

This module is DB-free and does no I/O beyond reading the config files
in :func:`load_macmath_dir`.
"""
import json
import logging
import os

from .mac import normalize_mac, mac_to_int, int_to_mac

log = logging.getLogger(__name__)

_VALID_SCHEMES = {"offset", "distinct_oui"}


def load_macmath_dir(path="/etc/flax/macmath"):
    """Read every ``<vid>.json`` in ``path`` -> ``{vid: config_dict}``.

    Soft-load: an absent dir yields ``{}``; a file whose stem is not an
    int, a malformed/unreadable file, or a config whose ``scheme`` is not
    in ``{"offset", "distinct_oui"}`` is skipped with a log warning rather
    than crashing.
    """
    configs = {}
    if not os.path.isdir(path):
        return configs

    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        stem = name[: -len(".json")]
        try:
            vid = int(stem)
        except ValueError:
            log.warning("macmath: skipping non-int filename %r", name)
            continue

        full = os.path.join(path, name)
        try:
            with open(full) as fh:
                cfg = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("macmath: skipping unreadable/malformed %r: %s",
                        full, exc)
            continue

        scheme = cfg.get("scheme") if isinstance(cfg, dict) else None
        if scheme not in _VALID_SCHEMES:
            log.warning("macmath: skipping %r: invalid scheme %r",
                        full, scheme)
            continue

        configs[vid] = cfg

    return configs


def _oui(mac):
    """Return the first 3 octets of a normalized mac, e.g. ``3c:2c:99``."""
    return normalize_mac(mac)[:8]


def classify_offset(observed, bmc_offset, host_offsets):
    """Offset-scheme classifier.

    ``observed`` is a list of already-normalized (colon-lowercase) macs.
    The family's MAC block base is taken as the lowest observed MAC; the
    BMC and each host NIC sit at ``base + <offset>``.

    Returns ``(bmc, hosts, junk)`` where ``hosts`` are the computed host
    macs that were actually observed (order-preserving, deduped) and
    ``junk`` is a list of ``{"mac": m, "reason": "macmath_offset_unmatched"}``
    dicts for observed macs that are neither the BMC nor a host.

    Contract: if the computed BMC mac was NOT actually observed, returns
    ``(None, [], [])`` so the caller falls back to the legacy pairing -- a
    BMC must be present, since seeing it is how the port was discovered.
    """
    base_int = min(mac_to_int(m) for m in observed)
    bmc = int_to_mac(base_int + bmc_offset)

    if bmc not in observed:
        return (None, [], [])

    observed_set = set(observed)
    hosts = []
    seen = set()
    for o in host_offsets:
        m = int_to_mac(base_int + o)
        if m in observed_set and m not in seen:
            hosts.append(m)
            seen.add(m)

    host_set = set(hosts)
    junk = [
        {"mac": m, "reason": "macmath_offset_unmatched"}
        for m in observed
        if m != bmc and m not in host_set
    ]
    return (bmc, hosts, junk)


def classify_distinct_oui(observed):
    """Distinct-OUI-scheme classifier.

    The BMC is the highest observed MAC. Host NIC(s) are observed macs
    whose OUI differs from the BMC's (foreign-OUI macs, e.g. the SONiC
    mgmt NIC). Same-OUI extras are BMC-side secondary interfaces and land
    in ``junk`` as ``{"mac": m, "reason": "macmath_same_oui_extra"}``.

    Returns ``(bmc, hosts, junk)``.
    """
    bmc = max(observed, key=mac_to_int)
    bmc_oui = _oui(bmc)

    hosts = [m for m in observed if m != bmc and _oui(m) != bmc_oui]
    junk = [
        {"mac": m, "reason": "macmath_same_oui_extra"}
        for m in observed
        if m != bmc and _oui(m) == bmc_oui
    ]
    return (bmc, hosts, junk)
