"""Serial-over-LAN capture: a node's console to a file for as long as a stage runs.

The console comes from the shared flax-post-sol relay (flax_post_sol/), never from a
`sol activate` of rackpost's own: a BMC takes one SOL payload, and a second session
only collects "SOL payload already active on another session" (L05's install
capture, 2026-10-30). The relay holds a session per BMC rackpost lists in
/rackpost/api/sol-targets and appends it to <capture dir>/<bmc ip>.txt.

start() POSTs the relay's /mark/<ip> (it rotates the file to .prev.txt, writes a
header and relaunches the session, so the capture starts on a fresh BMC-side SOL)
and follows that file from its start; a mark that fails is noted and the file is
followed from its current end. A thread copies new bytes into `path` (the order
dir's artifact) every SYNC_S, so rungs that read the artifact mid-stage (install's
sol_tail) see the console so far. A rotation in between (another mark, or the
relay's size cap) is followed by inode: the rest of the file now named .prev.txt is
copied first, then the new file from byte 0. stop() copies what is left and returns
the rung result. Read-only towards the node: no IPMI from here at all.
"""
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

from .result import RungResult

SOL_URL = os.environ.get("FLAX_POST_SOL_URL", "http://127.0.0.1:8448")
CAPTURE_DIR = os.environ.get("FLAX_POST_SOL_CAPTURE_DIR", "/var/lib/flax/post-sol")
SYNC_S = 2
CHUNK = 1 << 20
SOURCE = "flax-post-sol"


def relay_file(ip, capture_dir=None) -> Path:
    return Path(capture_dir or CAPTURE_DIR) / f"{ip}.txt"


def mark(ip, reason, *, timeout=5) -> dict:
    """POST /mark/<ip> to the relay (flax_post/observe/solclient.mark); never raises."""
    req = urllib.request.Request(f"{SOL_URL}/mark/{ip}", method="POST",
                                 data=json.dumps({"reason": reason}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:                  # noqa: BLE001 -- a dead relay is a note
        return {"ok": False, "reason": str(e)}


def _ino(p: Path):
    try:
        return p.stat().st_ino
    except OSError:
        return None


class Capture:
    """One running capture; stop() it exactly once (later calls are no-ops)."""

    def __init__(self, ip, path, capture_dir=None):
        self.ip, self.path = ip, Path(path)
        self.src = relay_file(ip, capture_dir)
        self.prev = self.src.with_name(f"{ip}.prev.txt")
        self.ino = None                     # the relay file being followed
        self.off = 0                        # bytes of it already copied
        self.rotations = 0
        self.lost = 0                       # rotations whose old file was gone again
        self.error: str | None = None       # the capture never started
        self.note: str | None = None
        self.mark_err: str | None = None    # the relay's mark failed (why)
        self.started = time.time()
        self._mu = threading.Lock()
        self._stop = threading.Event()
        self._out = None
        self._watch = None
        self.result: RungResult | None = None

    def _copy(self, src: Path, off: int) -> int:
        """Append src[off:] to the artifact; the new offset."""
        try:
            with open(src, "rb") as f:
                f.seek(off)
                while True:
                    b = f.read(CHUNK)
                    if not b:
                        break
                    self._out.write(b)
                    off += len(b)
        except OSError:
            pass
        self._out.flush()
        return off

    def sync(self) -> None:
        with self._mu:
            if self._out is None:
                return
            ino = _ino(self.src)
            if ino is None:
                return
            if ino != self.ino:
                if self.ino is not None:    # rotated: finish the file we were following
                    self.rotations += 1
                    if _ino(self.prev) == self.ino:
                        self._copy(self.prev, self.off)
                    else:
                        self.lost += 1
                self.ino, self.off = ino, 0
            self.off = self._copy(self.src, self.off)

    def _run(self):
        while not self._stop.wait(SYNC_S):
            self.sync()

    def stop(self) -> RungResult:
        """Copy the rest and close the artifact; pass when it holds any bytes."""
        if self.result is not None:
            return self.result
        self._stop.set()
        if self._watch is not None:
            self._watch.join(SYNC_S + 5)
        self.sync()
        with self._mu:
            if self._out is not None:
                self._out.close()
                self._out = None
        try:
            n = self.path.stat().st_size
        except OSError:
            n = 0
        detail = {"bytes": n, "source": f"{SOURCE} {self.src}",
                  "seconds": round(time.time() - self.started), "rotations": self.rotations}
        notes = [x for x in (self.note, self.lost and
                             f"{self.lost} rotation(s) outran the copy: console missing there")
                 if x]
        if notes:
            detail["note"] = "; ".join(notes)
        if self.error:
            detail["error"] = self.error
        elif not n:
            detail["error"] = (f"no sol capture (0 bytes from {SOURCE})"
                               + (f"; relay mark failed ({self.mark_err})" if self.mark_err
                                  else ""))
        self.result = RungResult("fail" if "error" in detail else "pass", detail)
        return self.result


def start(ip, path, *, reason="rackpost", capture_dir=None, mark_fn=None) -> Capture:
    """Mark the relay and begin copying `ip`'s console into `path` (appended). Never
    raises: a capture that could not start carries .error and its stop() fails."""
    cap = Capture(ip, path, capture_dir)
    try:
        cap.path.parent.mkdir(parents=True, exist_ok=True)
        cap._out = open(cap.path, "ab")
    except OSError as e:
        cap.error = f"sol artifact: {e}"
        return cap
    m = (mark_fn or mark)(ip, reason)
    if m.get("ok"):
        cap.ino = _ino(cap.src)             # the fresh file, from its header on
    else:
        cap.mark_err = str(m.get("reason"))
        cap.note = f"relay mark failed ({cap.mark_err}); followed from the current end"
        cap.ino = _ino(cap.src)
        try:
            cap.off = cap.src.stat().st_size if cap.ino is not None else 0
        except OSError:
            cap.off = 0
    cap._watch = threading.Thread(target=cap._run, daemon=True, name=f"sol-{ip}")
    cap._watch.start()
    return cap


def stop(cap: Capture) -> RungResult:
    return cap.stop()


def tail(ip, *, offset=None, ino=None, max_bytes=64 * 1024, capture_dir=None) -> dict:
    """The relay file for the UI's live viewer: bytes from `offset` (same inode), else
    the last max_bytes. {ino, offset (the new end), reset, text}; ino None = no file."""
    p = relay_file(ip, capture_dir)
    try:
        with open(p, "rb") as f:
            st = os.fstat(f.fileno())
            size = st.st_size
            reset = offset is None or ino != st.st_ino or offset > size
            start_at = max(0, size - max_bytes) if reset else max(offset, size - 4 * max_bytes)
            reset = reset or start_at > offset      # too far behind: a fresh tail
            f.seek(start_at)
            b = f.read(size - start_at)
    except OSError:
        return {"ino": None, "offset": 0, "reset": True, "text": ""}
    return {"ino": st.st_ino, "offset": start_at + len(b), "reset": reset,
            "text": b.decode("utf-8", "replace")}
