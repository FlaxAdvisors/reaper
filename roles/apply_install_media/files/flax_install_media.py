#!/usr/bin/env python3.12
"""Make this bang hold every ISO in the install-media manifest, sha256-verified.

A verified ISO is never touched (its checksum is cached beside it as <iso>.sha256 and
reused while size+mtime hold). A missing or wrong one is fetched into <iso>.part from its
upstream url, else copied bang-to-bang from the first peer that has it, verified, then
renamed into place: an unverified file never takes the ISO's name. Prints one JSON object:
{"status": {path: state}, "remount": [mount targets whose loop source is another file]}.
Owner: reaper apply_install_media (spec 2026-11-10-install-files-into-reaper)."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

MARGIN = 1 << 30            # keep 1 GiB free beyond the ISO itself


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def verified(iso: str, sha: str, size: int, write_cache: bool = True) -> bool:
    try:
        st = os.stat(iso)
    except OSError:
        return False
    if st.st_size != size:
        return False
    cache = iso + ".sha256"
    stamp = f"{st.st_size} {st.st_mtime_ns}"
    try:
        c_sha, c_stamp = open(cache).read().split(" ", 1)
        if c_stamp.strip() == stamp:
            return c_sha == sha
    except (OSError, ValueError):
        pass
    got = _sha256(iso)
    if not write_cache:
        return got == sha
    try:
        with open(cache, "w") as f:
            f.write(f"{got} {stamp}\n")
    except OSError:
        pass
    return got == sha


def ensure(entry: dict, *, root: str, peers, fetch_url, fetch_peer, free_bytes, peer_sha=None) -> str:
    iso = os.path.join(root, entry["path"])
    if verified(iso, entry["sha256"], entry["size"]):
        return "present"
    os.makedirs(os.path.dirname(iso), exist_ok=True)
    if free_bytes(os.path.dirname(iso)) < entry["size"] + MARGIN:
        return f"FAIL: not enough space for {entry['size']} bytes"
    part = iso + ".part"
    sources = ([("url", entry["url"])] if entry.get("url") else []) + [("peer", p) for p in peers]
    for kind, src in sources:
        if kind == "peer" and peer_sha is not None:
            try:
                theirs = peer_sha(src, entry["path"])
            except Exception:  # noqa: BLE001 -- unreachable peer: next one
                continue
            if theirs is not None and theirs != entry["sha256"]:
                continue            # it holds another file (e.g. an older re-spin): never stream it
        try:
            ok = fetch_url(src, part) if kind == "url" else fetch_peer(src, entry["path"], part)
        except Exception:  # noqa: BLE001 -- one bad source must not stop the next
            ok = False
        if ok and os.path.exists(part) and os.path.getsize(part) == entry["size"] \
                and _sha256(part) == entry["sha256"]:
            os.replace(part, iso)
            for stale in (iso + ".sha256",):
                if os.path.exists(stale):
                    os.remove(stale)
            verified(iso, entry["sha256"], entry["size"])
            return "fetched" if kind == "url" else f"copied from {src}"
        if os.path.exists(part):
            os.remove(part)
    return "FAIL: no source produced a verified copy"


def needs_remount(iso: str, source: "str | None") -> bool:
    """A mount is redone only when it is mounted from a different file; never when it is
    already this ISO, and an unmounted target is simply started."""
    return source is not None and os.path.normpath(source) != os.path.normpath(iso)


def mount_source(target: str) -> "str | None":
    r = subprocess.run(["findmnt", "-n", "-o", "SOURCE", target], capture_output=True, text=True)
    dev = r.stdout.strip()
    if not dev:
        return None
    if dev.startswith("/dev/loop"):
        b = subprocess.run(["losetup", "-n", "-O", "BACK-FILE", dev], capture_output=True, text=True)
        return b.stdout.strip() or dev
    return dev


def curl_argv(url: str, part: str) -> list:
    # a stalled WAN transfer (< 100 KiB/s for 2 min) is abandoned, never waited on forever
    return ["curl", "-fsSL", "--retry", "3", "--connect-timeout", "20",
            "--speed-limit", "102400", "--speed-time", "120", "-o", part, url]


def peer_argv(user: str, host: str, remote: str) -> list:
    return ["sudo", "-n", "-u", user, "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", f"{user}@{host}", "cat", remote]


def _curl(url: str, part: str) -> bool:
    return subprocess.run(curl_argv(url, part)).returncode == 0


def _peer(user: str, root: str):
    def fetch(host: str, path: str, part: str) -> bool:
        with open(part, "wb") as out:
            return subprocess.run(peer_argv(user, host, os.path.join(root, path)), stdout=out).returncode == 0

    def sha(host: str, path: str) -> "str | None":
        """The peer's own verified checksum (its <iso>.sha256 cache); None when unknown."""
        r = subprocess.run(peer_argv(user, host, os.path.join(root, path) + ".sha256"),
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise OSError(f"{host}: no cached checksum")
        return (r.stdout.split() or [None])[0]
    return fetch, sha


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--root", default="/export/isos")
    ap.add_argument("--mount-root", default="/export/install")
    ap.add_argument("--peer", action="append", default=[])
    ap.add_argument("--peer-user", default="flax")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    entries = json.load(sys.stdin if a.manifest == "-" else open(a.manifest))
    fetch_peer, peer_sha = _peer(a.peer_user, a.root)
    status, remount = {}, []
    for e in entries:
        iso = os.path.join(a.root, e["path"])
        if a.check:
            status[e["path"]] = "present" if verified(iso, e["sha256"], e["size"], write_cache=False) \
                else "would fetch"
        else:
            status[e["path"]] = ensure(e, root=a.root, peers=a.peer, fetch_url=_curl,
                                       fetch_peer=fetch_peer, peer_sha=peer_sha,
                                       free_bytes=lambda d: shutil.disk_usage(d).free)
        if e.get("mount") and needs_remount(iso, mount_source(os.path.join(a.mount_root, e["mount"]))):
            remount.append(e["mount"])
    print(json.dumps({"status": status, "remount": remount}))
    return 1 if any(str(v).startswith("FAIL") for v in status.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
