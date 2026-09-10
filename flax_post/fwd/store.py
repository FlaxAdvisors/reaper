"""Per-port store for the post firmware driver: /etc/flax/post_fw.json.

The driver is the sole writer; the Plan-1 viewer (flax_post.fw_store) reads it.
Row shape mirrors the existing bmcfw.json:
{port, bmc_ip, current_version, target_version, phase, percent, fault_reason, updated_at}.

Write strategy: in production post_fw.json is a docker *file* bind-mount, and you
cannot rename over a mount point — os.replace(tmp, path) fails with EBUSY
("Device or resource busy"). So we write IN PLACE (truncate + single write)
rather than the usual tmp+rename. A reader can therefore catch a torn write; the
viewer tolerates that (json parse error -> {} -> re-poll in ~15s), so the lost
rename-atomicity is acceptable. The full JSON is serialized before the file is
opened to keep the truncate->write window as small as possible.

target_version is NOT trusted as frozen per-row truth on read (see `read()`):
a row only gets a fresh target_version/phase when it is actively re-probed, so a
port that stops being reachable freezes whatever the manifest said the LAST time
it was probed. If the manifest's target later changes, that frozen row silently
disagrees with it forever -- confirmed live 2026-09-10 on eindhoven, several
`unreachable` rows still showing a pre-2026-09-09 target after the manifest was
bumped. `read()` overlays the CURRENT manifest's target onto every row (and
re-derives up_to_date/needs_update from it) so every consumer -- this module's
own set_row RMW, the fleet viewer, anything else that reads this file -- sees a
target that can never go stale, without each of them re-implementing the fix.
"""
import json
import os
import threading
import time

from . import config, manifest

STORE_PATH = os.environ.get("FLAX_POST_STORE", "/etc/flax/post_fw.json")

# set_row is a read-modify-write of the whole file; the fanned-out probe pass and
# the async flash thread call it concurrently, so the RMW must be serialized
# in-process or rows are lost to clobbering. (The file is the sole store; one
# process owns it, so a process-local lock is sufficient.)
_LOCK = threading.Lock()

# Phases that are a version-compare VERDICT, not a procedural/terminal state --
# only these get re-derived against a fresh target. Recomputing "unreachable"
# would assert a confidence about the node's live state that the row cannot
# currently back up (its current_version could be arbitrarily old); recomputing
# "fault"/"oem"/"done"/"checking"/"flashing"/"monitoring"/"activating" would
# overwrite meaning those phases carry beyond "does the version match".
_VERDICT_PHASES = ("up_to_date", "needs_update")


def _overlay_live_target(store: dict) -> dict:
    """Mutate + return `store`: refresh target_version (and, for a plain verdict
    row, phase) against the manifest's CURRENT target. Fails open -- a missing/
    malformed manifest, or a matcher with no match, leaves every row exactly as
    stored (better a frozen-but-plausible target than none at all)."""
    hit = manifest.PostMatcher(manifest.load_manifest(config.CONFIG_DIR)).match()
    if hit is None:
        return store
    live_target = manifest.target_version(hit[1])
    for row in store.values():
        if not isinstance(row, dict) or "target_version" not in row:
            continue
        row["target_version"] = live_target
        if row.get("phase") in _VERDICT_PHASES and row.get("current_version"):
            row["phase"] = ("up_to_date"
                            if manifest.compare(row["current_version"], live_target) == "same"
                            else "needs_update")
    return store


def _load_raw() -> dict:
    try:
        with open(STORE_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read() -> dict:
    return _overlay_live_target(_load_raw())


def _write(store: dict) -> None:
    # In-place write — post_fw.json is a file bind-mount; os.replace() over it
    # fails with EBUSY. Serialize first, then a single truncate+write.
    data = json.dumps(store)
    with open(STORE_PATH, "w") as f:
        f.write(data)


def sweep(live_ports) -> list:
    """Delete any row whose port is not in `live_ports` (the current post BMC
    reservation set). Returns the ports actually removed.

    No grace period, unlike post_state's FDB-based GC (flax_post/observe/gc.py):
    that latch exists because a BMC MAC can transiently drop off the switch FDB
    mid-reboot while the reservation itself is still live, and GC-ing THAT would
    be a false positive. Here the input is the reservation list itself -- once a
    port has no source=post reservation at all, it is not coming back under that
    identity, so there is nothing to wait out. Left alone, a stale row can only
    ever go further stale (frozen current/target/phase) from that point on --
    confirmed live 2026-09-10: several `unreachable` rows for long-unpopulated
    slots were still carrying an August target_version months later."""
    live = set(live_ports)
    with _LOCK:
        data = _load_raw()
        stale = [p for p in data if p not in live]
        if not stale:
            return []
        for p in stale:
            del data[p]
        _write(data)
        return stale


def set_row(port: str, **fields) -> dict:
    """Merge `fields` (+ updated_at) into store[port]; write; return the row.

    The read-modify-write is held under _LOCK so concurrent writers (parallel
    probe workers + the flash thread) don't clobber each other's rows."""
    with _LOCK:
        store = _load_raw()
        row = store.get(port) or {}
        row.update(fields)
        row["port"] = port
        row["updated_at"] = time.time()
        store[port] = row
        _write(store)
        return row
