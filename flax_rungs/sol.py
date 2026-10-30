"""Serial-over-LAN capture: a node's console to a file for as long as a stage runs.

start() opens `ipmitool -I lanplus -C <cipher> -H <ip> -U <user> -E sol activate`
with its output appended to a file (the order dir's artifact); stop() ends it with
`sol deactivate` and returns the rung result. The credential x cipher pair is the
first whose `chassis power status` answers (bmc._session: 17, then 3), and the
password rides in IPMI_PASSWORD (-E), never in argv. A stale session left by an
earlier capture or a human is deactivated first, else activate is refused.

ipmitool exits when the BMC drops the SOL payload (a BMC reboot, a session
timeout); the capture re-activates once per RELAUNCH_S, at most MAX_RELAUNCH
times, so the file covers the whole stage like the flax-post-sol relay's. stop()
always sends `sol deactivate` once a session was opened, whatever went wrong.
Read-only towards the node: SOL never power-controls anything.
"""
import os
import subprocess
import threading
import time
from pathlib import Path

from . import bmc
from .result import RungResult

RELAUNCH_S = 10
MAX_RELAUNCH = 30
STOP_GRACE_S = 5


def _argv(ip, user, cipher, *args) -> list[str]:
    return ["ipmitool", "-I", "lanplus", "-C", str(cipher), "-H", ip, "-U", user, "-E", "sol",
            *args]


class Capture:
    """One running SOL capture; stop() it exactly once (later calls are no-ops)."""

    def __init__(self, ip, path, *, runner, popen):
        self.ip, self.path = ip, Path(path)
        self.runner, self.popen = runner, popen
        self.user = self.pw = self.cipher = None
        self.proc = None
        self.error: str | None = None      # the capture never started
        self.note: str | None = None       # a re-activate failed (what came before stands)
        self.drops: list[int] = []          # exit codes of sessions the BMC ended
        self.started = time.time()
        self._mu = threading.Lock()
        self._stop = threading.Event()
        self._out = None
        self._watch = None
        self.result: RungResult | None = None

    def _env(self) -> dict:
        env = dict(os.environ)
        env["IPMI_PASSWORD"] = self.pw     # -E reads it; never -P (argv)
        return env

    def _deactivate(self):
        try:
            self.runner(_argv(self.ip, self.user, self.cipher, "deactivate"),
                        capture_output=True, text=True, timeout=20, env=self._env())
        except (subprocess.TimeoutExpired, OSError):
            pass

    def _spawn(self):
        # stdin stays an open pipe nobody writes: ipmitool reads it for its escape
        # sequence, and EOF there would end the session
        return self.popen(_argv(self.ip, self.user, self.cipher, "activate"),
                          stdin=subprocess.PIPE, stdout=self._out, stderr=subprocess.STDOUT,
                          env=self._env())

    def _supervise(self):
        while not self._stop.is_set():
            proc = self.proc
            rc = proc.wait()
            if self._stop.is_set():
                return
            self.drops.append(rc)
            if len(self.drops) > MAX_RELAUNCH or self._stop.wait(RELAUNCH_S):
                return
            with self._mu:
                if self._stop.is_set():
                    return
                try:
                    self.proc = self._spawn()
                except OSError as e:
                    self.note = f"sol re-activate: {e}"
                    return

    def stop(self) -> RungResult:
        """Deactivate, end ipmitool, close the file; pass when it holds any bytes."""
        if self.result is not None:
            return self.result
        self._stop.set()
        try:
            with self._mu:
                proc = self.proc
            if self.user is not None:
                self._deactivate()          # always, once a session was opened
            if proc is not None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.wait(timeout=STOP_GRACE_S)
                except (subprocess.TimeoutExpired, OSError):
                    proc.kill()
                    try:
                        proc.wait(timeout=STOP_GRACE_S)
                    except subprocess.TimeoutExpired:
                        pass
            if self._watch is not None:
                self._watch.join(STOP_GRACE_S)
        finally:
            if self._out is not None:
                self._out.close()
        try:
            n = self.path.stat().st_size
        except OSError:
            n = 0
        detail = {"bytes": n, "cipher": self.cipher, "user": self.user,
                  "seconds": round(time.time() - self.started), "drops": len(self.drops)}
        if self.note:
            detail["note"] = self.note
        if self.error:
            detail["error"] = self.error
        elif not n:
            detail["error"] = "no sol capture (0 bytes)"
        self.result = RungResult("fail" if "error" in detail else "pass", detail)
        return self.result


def start(ip, creds, path, *, runner=subprocess.run, popen=subprocess.Popen) -> Capture:
    """Begin capturing `ip`'s SOL into `path` (appended). Never raises: a capture that
    could not start carries .error and its stop() fails with it."""
    cap = Capture(ip, path, runner=runner, popen=popen)
    s = bmc._session(ip, creds, runner=runner)
    if s is None:
        cap.error = "no BMC session for sol (every credential x cipher refused)"
        return cap
    cap.user, cap.pw, cap.cipher, _ = s
    cap._deactivate()                       # a stale session refuses a new activate
    try:
        cap.path.parent.mkdir(parents=True, exist_ok=True)
        cap._out = open(cap.path, "ab")
        cap.proc = cap._spawn()
    except OSError as e:
        cap.error = f"sol activate: {e}"
        return cap
    cap._watch = threading.Thread(target=cap._supervise, daemon=True,
                                  name=f"sol-{ip}")
    cap._watch.start()
    return cap


def stop(cap: Capture) -> RungResult:
    return cap.stop()
