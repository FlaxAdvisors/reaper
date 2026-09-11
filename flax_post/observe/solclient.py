# flax_post/observe/solclient.py
"""flax-post-sol client for the slot ladder (spec 2026-09-11 post-slot-ladder §8):
mark() rotates a slot's capture file (and relaunches a dead session); read_capture()
reads the current file for the console artifact. Loopback only; never raises."""
import json
import os
import urllib.request

SOL_URL = os.environ.get("FLAX_POST_SOL_URL", "http://127.0.0.1:8448")
CAPTURE_DIR = os.environ.get("FLAX_POST_SOL_CAPTURE_DIR", "/var/lib/flax/post-sol")


def _default_transport(method, url, body, timeout):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def mark(bmc_ip, reason, *, transport=None, timeout=5) -> dict:
    t = transport or _default_transport
    try:
        code, text = t("POST", f"{SOL_URL}/mark/{bmc_ip}", {"reason": reason}, timeout)
        if code != 200:
            return {"ok": False, "reason": f"http {code}"}
        return json.loads(text)
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def read_capture(bmc_ip, *, capture_dir=None, max_bytes=4 * 1024 * 1024):
    path = os.path.join(capture_dir or CAPTURE_DIR, f"{bmc_ip}.txt")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                return "[truncated]\n" + f.read().decode("utf-8", "replace")
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    return text or None
