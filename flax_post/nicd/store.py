"""Per-port store /etc/flax/post_nic_fw.json — sole writer, twin of
flax_post.biosd.store. In-place write (docker file bind-mount -> os.replace
EBUSY); a process-local lock serializes the read-modify-write across the
fanned-out probe workers and the enforce flash."""
import json
import os
import threading
import time

STORE_PATH = os.environ.get("FLAX_POST_NIC_STORE", "/etc/flax/post_nic_fw.json")
_LOCK = threading.Lock()


def read() -> dict:
    try:
        with open(STORE_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(store: dict) -> None:
    data = json.dumps(store)
    with open(STORE_PATH, "w") as f:
        f.write(data)


def set_row(port: str, **fields) -> dict:
    with _LOCK:
        store = read()
        row = store.get(port) or {}
        row.update(fields)
        row["port"] = port
        row["updated_at"] = time.time()
        store[port] = row
        _write(store)
        return row
