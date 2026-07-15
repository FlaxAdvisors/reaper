"""Pure config-in-use map: each /etc/flax file -> what it decides, who reads
it, and whether it's live. 'drift' = file mtime newer than the newest ack
among its readers (a config changed but not yet picked up). A heuristic hint,
not a guarantee — a service can ack without re-reading a file."""
import datetime


def _epoch(iso):
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def build_config_map(files, acks, *, now_ts):
    ack_epoch = {}
    for a in acks or []:
        e = _epoch(a.get("consumed_at"))
        if e is not None:
            prev = ack_epoch.get(a.get("consumer"))
            ack_epoch[a.get("consumer")] = e if prev is None else max(prev, e)
    out = []
    for f in files:
        mtime = f.get("mtime")
        if mtime is None:
            state = "absent"
        else:
            reader_acks = [ack_epoch[r] for r in f.get("readers", []) if r in ack_epoch]
            newest_ack = max(reader_acks) if reader_acks else None
            state = "drift" if (newest_ack is not None and mtime > newest_ack) else "live"
        out.append({**f, "live_state": state})
    return out


# (name, decides, readers) — readers use consumer_acks names; "control" reads
# are elided from drift (flax-control has no ack row). Mirrors the compose
# mounts + docs/flax-api-impl.md consumer map.
CATALOGUE = [
    ("geometry.json", "Triage membership — which ports are the triage role", ["flax-classify"]),
    ("post-geometry.json", "Post rack/slot layout & reservation prefixes", ["flax-classify"]),
    ("vlans.json", "(family, phase) → vid (registry-first fallback)", ["flax-classify","flax-observe","flax-reconcile"]),
    ("switches.json", "Switch driver + reachability inventory", ["flax-switch-sense","flax-reconcile"]),
    ("no-steer-ports.json", "Ports classify must never vid-steer", ["flax-classify","flax-observe","flax-reconcile"]),
    ("turtle-geometry.json", "Cumulus OOB-mgmt swp → BMC slot", []),
    ("bmc-only-families.json", "Families that are BMC-only", ["flax-classify"]),
    ("reconcile.json", "Reconcile tunables", ["flax-reconcile"]),
    ("bmc-firmware-versions.json", "Target BMC/NIC firmware manifest", []),
]


def catalogue_with_mtimes(config_dir, stat_fn):
    """Build the files list for build_config_map: attach each catalogue file's
    mtime via stat_fn(path)->float|None (injected for testability)."""
    import os
    out = []
    for name, decides, readers in CATALOGUE:
        out.append({"name": name, "decides": decides, "readers": readers,
                    "mtime": stat_fn(os.path.join(config_dir, name))})
    return out
