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
"""
import json
import os
import threading
import time

STORE_PATH = os.environ.get("FLAX_POST_STORE", "/etc/flax/post_fw.json")

# set_row is a read-modify-write of the whole file; the fanned-out probe pass and
# the async flash thread call it concurrently, so the RMW must be serialized
# in-process or rows are lost to clobbering. (The file is the sole store; one
# process owns it, so a process-local lock is sufficient.)
_LOCK = threading.Lock()


def read() -> dict:
    try:
        with open(STORE_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(store: dict) -> None:
    # In-place write — post_fw.json is a file bind-mount; os.replace() over it
    # fails with EBUSY. Serialize first, then a single truncate+write.
    data = json.dumps(store)
    with open(STORE_PATH, "w") as f:
        f.write(data)


def set_row(port: str, **fields) -> dict:
    """Merge `fields` (+ updated_at) into store[port]; write; return the row.

    The read-modify-write is held under _LOCK so concurrent writers (parallel
    probe workers + the flash thread) don't clobber each other's rows."""
    with _LOCK:
        store = read()
        row = store.get(port) or {}
        row.update(fields)
        row["port"] = port
        row["updated_at"] = time.time()
        store[port] = row
        _write(store)
        return row
