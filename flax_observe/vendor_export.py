"""Export bmc_vendor to /etc/flax/bmc_vendor.json for host-side bins.

flax-observe (the sole writer) is the only service that talks to a BMC and
classifies its firmware vendor (see flax_observe.bmc_vendor). ghost's bins --
a separate repo, phase 4 of the BMC-vendor plan -- already read a dozen or so
`/etc/flax/*.json` files on the bang host; this file lets them pick up
`bmc_vendor` directly, without an HTTP round-trip to the control API. A typical
read looks like:

    jq -r '.[] | select(.bmc_ip=="172.17.6.101") | .vendor' /etc/flax/bmc_vendor.json

A port with no row here should be read as vendor "unknown" -- absence is not
an error, it just means this run of observe never (yet) probed that port to
a firmware vendor.

RULE ZERO: this file carries vendor, switch, port, bmc_ip, bmc_mac and
`updated_at` -- and NOTHING else. In particular it must never carry
`creds_used` (or any other field out of the raw bmc_kind_cached probe dict);
callers must pass only the six documented fields into `update()`.

Write strategy mirrors flax_post/fwd/store.py: in production this file is a
docker *file* bind-mount (roles/apply_flax_observe/templates/
flax-observe.service.j2), so os.replace(tmp, path) over it fails EBUSY
("Device or resource busy"), and swapping the inode would leave any reader
that already has the file open (or another bind-mount view of it) pinned to a
stale snapshot. So writes are IN PLACE: the whole store is serialized to a
string first, then a single truncate+write -- no temp file, no rename.

Consequence for readers: truncate+write is NOT atomic from a concurrent
reader's point of view. A reader that opens/reads this file at exactly the
wrong instant can see it empty (right after truncate, before the write lands)
or partial/malformed JSON (mid-write). This is a normal, expected race given
the write strategy above, not a bug to fix here. A consumer must treat a
`json.loads` failure (or a suspiciously-empty read) as "retry shortly", never
as "there are no rows" -- the file has real content almost all the time; the
race window is a single write() call.

`updated_at` is "when the vendor/ip/mac last CHANGED", not "when this port was
last seen". Workers cycle every ~10s across ~80 ports; rewriting the file on
every cycle for rows that haven't changed would mean constant disk I/O and
constant churn for anything watching the file's mtime. `update()` compares
the incoming row against the stored one (ignoring updated_at) and only writes
when the content actually differs.

The vendor cache (port_state["bmc_kind_cached"]) is NOT hydrated across an
observe restart, so the constructor loads whatever this file already has on
disk into memory -- otherwise every restart would blank the file until each
port got re-probed.

Row lifetime and duplicate bmc_ip note: a row for a port that leaves the
dynamic access-port set at RUNTIME (the supervisor's reconcile_workers, when
a port is no longer desired) is actively dropped -- see
flax_observe.__main__.reconcile_workers, the one call site that can tell
"removed" apart from "process shutdown" (a plain restart leaves every row
in place on purpose, per the constructor note above). But a row loaded from
disk at CONSTRUCTION time for a port that is no longer enrolled in this run
(e.g. geometry shrank between two observe restarts) is not swept -- it just
sits there, unrefreshed, until either that exact switch:port key is observed
again or something else calls `remove()` for it. If a BMC's bmc_ip is later
reassigned to a different port before the old row is cleaned up, a naive
`select(.bmc_ip=="...")` can therefore match MORE THAN ONE row. A consumer
should prefer the newest `updated_at`, e.g.:

    jq -r '[.[] | select(.bmc_ip=="172.17.6.101")] | sort_by(.updated_at) | last | .vendor' \
        /etc/flax/bmc_vendor.json

Freshness note: flax-observe runs only on the bang currently holding the MGMT
VIP (flax-observe.service's ExecStartPre VIP-gate). With a `--limit
bang-gouda` deploy, the standby (bang-edam) has NO file at all -- it has never
run the code that writes it, and runs the old unit instead. It does not hold
"possibly `{}`"; that would imply a version of flax-observe ran there and
wrote an empty store, which has not happened. Do not read this file on the
standby and expect it to exist, let alone be current.
"""
import json
import logging
import os
import threading
import datetime


log = logging.getLogger("flax-observe.vendor_export")

STORE_PATH = os.environ.get("FLAX_OBSERVE_VENDOR_STORE", "/etc/flax/bmc_vendor.json")

# The row fields written for every port (besides updated_at). Used both to
# build a row and to compare two rows "ignoring updated_at".
_ROW_FIELDS = ("switch", "port", "vendor", "bmc_ip", "bmc_mac")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: str) -> dict:
    """Load the store from disk. Tolerates a missing or malformed file (never
    raises) -- either just means "nothing persisted yet"."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: str, store: dict) -> None:
    """In-place write: serialize first, then a single truncate+write. May
    raise OSError; callers are responsible for catching it (see
    VendorExport._write_locked) -- this module function stays a thin,
    easily-monkeypatched seam for tests."""
    data = json.dumps(store)
    with open(path, "w") as f:
        f.write(data)


class VendorExport:
    """The sole in-process handle onto /etc/flax/bmc_vendor.json.

    One instance is shared by every PortWorker thread (flax_observe/
    __main__.py sets it once as env.vendor_export); all read-modify-write
    access goes through `_lock`, a process-local threading.Lock -- this
    process is the only writer, so that is sufficient (see
    flax_post/fwd/store.py for the same reasoning).
    """

    def __init__(self, path: str = STORE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._store: dict = _load(path)
        # Last write-failure message logged, so a persistently broken mount
        # logs once at WARNING instead of once per ~10s cycle per port.
        self._last_write_error: str | None = None

    def _row_matches(self, existing: dict, vendor, bmc_ip, bmc_mac) -> bool:
        return (existing.get("vendor") == vendor
                and existing.get("bmc_ip") == bmc_ip
                and existing.get("bmc_mac") == bmc_mac)

    def _write_locked(self) -> None:
        """Write self._store to disk. Never raises -- an export failure must
        never break the core port cycle (architecture rule 3)."""
        try:
            _write(self.path, self._store)
        except OSError as e:
            msg = str(e)
            if msg != self._last_write_error:
                log.warning("bmc_vendor.json write to %s failed: %s",
                           self.path, msg)
                self._last_write_error = msg

    def update(self, switch: str, port: str, *, vendor, bmc_ip, bmc_mac) -> None:
        """Upsert the row for (switch, port).

        - bmc_mac falsy (port empty / identity forgotten) -> the row is
          removed, regardless of `vendor`.
        - Else, vendor falsy (not yet (re-)probed this run) -> any existing
          row is left untouched; there is nothing fresh to record.
        - Else the row is set to exactly {switch, port, vendor, bmc_ip,
          bmc_mac, updated_at}, replacing whatever was there (including a row
          that named a different bmc_mac for this switch:port). The file is
          only rewritten when the row's content actually changed -- workers
          cycle every ~10s across ~80 ports, and most cycles see no vendor/
          ip/mac movement at all.
        """
        key = f"{switch}:{port}"
        with self._lock:
            if not bmc_mac:
                if self._store.pop(key, None) is not None:
                    self._write_locked()
                return
            if not vendor:
                return
            existing = self._store.get(key)
            if existing is not None and self._row_matches(existing, vendor,
                                                           bmc_ip, bmc_mac):
                return
            self._store[key] = {
                "switch": switch,
                "port": port,
                "vendor": vendor,
                "bmc_ip": bmc_ip,
                "bmc_mac": bmc_mac,
                "updated_at": _now_iso(),
            }
            self._write_locked()

    def remove(self, switch: str, port: str) -> None:
        """Drop the row for (switch, port), writing only if it existed."""
        key = f"{switch}:{port}"
        with self._lock:
            if self._store.pop(key, None) is not None:
                self._write_locked()
