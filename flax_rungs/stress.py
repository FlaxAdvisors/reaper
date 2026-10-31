"""Deploy's stress: stress-ng on every installed node at once, judged by the BMC's SEL,
the kernel log and stress-ng's own exit code.

  before   per node, in parallel: stress-ng apt-installed where missing (it stays
           installed, like iperf3), the node's uptime noted (T0 for the dmesg delta),
           and out of band `sdr elist` + `sel elist` (endsummary.read_oob: the pair
           whose power status answers; IPMI_PASSWORD via -E, never argv)
  launch   every node at once (one thread each, released together):
           `stress-ng --cpu 0 --vm VM_WORKERS --vm-bytes 80% --timeout <s>s
           --metrics-brief` detached (setsid) on the node, output + exit code under /run;
           --timeout ends it on its own, so a daemon that dies cannot leave it running
  wait     <s> seconds (in POLL_S steps: Cancel stops stress-ng on every node with the
           deadline's pkill and skips the after ssh, an `unmeasured`), then the exit-code
           files are polled until every node has one or the deadline (counted from the
           launch, not the prep's apt-get) passes (stress-ng still running then is killed:
           a fail)
  after    per node: stress-ng's output + exit code and the kernel log lines stamped after
           T0 (one ssh), then SDR + SEL again

Verdict per node: `fail` on stress-ng rc != 0 (or no exit code by the deadline), any
DMESG_BAD line in the delta, a node not answering ssh after the run, or a new SEL line
(in the after read, not the before) that is not benign (endsummary.nonbenign: BENIGN_SEL
and the 60 s Asserted/Deasserted threshold transient rule); `unmeasured` when the node
did not answer ssh before anything ran, or a SEL read failed (SEL not read is not SEL
clean) and nothing else was wrong; else `pass`. Nothing here power-controls the node or
clears its SEL. The artifacts are stress-ng's output, the dmesg delta and the SDR/SEL
before and after."""
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import endsummary, hostssh
from .remote import unreachable
from .result import UNMEASURED, RungResult

SECONDS = 600
VM_WORKERS = 4
DEADLINE_S = 1200
POLL_S = 15
DMESG_BAD = ("mce:", "Hardware Error", "EDAC", "temperature above threshold", "Machine check")
_OUT, _RC = "/run/flax-deploy-stress.out", "/run/flax-deploy-stress.rc"
_MARK = "===flax-stress-"


def command(seconds: int = SECONDS, vm: int = VM_WORKERS) -> str:
    return (f"stress-ng --cpu 0 --vm {vm} --vm-bytes 80% --timeout {int(seconds)}s "
            "--metrics-brief")


_PREP = (f"rm -f {_OUT} {_RC}\n"
         "command -v stress-ng >/dev/null || DEBIAN_FRONTEND=noninteractive "
         "apt-get -o DPkg::Lock::Timeout=120 install -y -q stress-ng >/dev/null 2>&1\n"
         "command -v stress-ng >/dev/null && echo HAVE || echo NOSTRESS\n"
         "echo T0 $(cut -d' ' -f1 /proc/uptime)\n")


def _launch(cmd: str) -> str:
    inner = f"{cmd} >{_OUT} 2>&1; echo $? >{_RC}"
    return f"setsid sh -c '{inner}' </dev/null >/dev/null 2>&1 &\necho STARTED\n"


_DONE = f"test -s {_RC} && echo DONE || echo RUNNING\n"
_COLLECT = (f"echo '{_MARK}out==='\ncat {_OUT} 2>/dev/null\n"
            f"echo '{_MARK}rc==='\ncat {_RC} 2>/dev/null\n"
            f"echo '{_MARK}dmesg==='\ndmesg 2>&1\n")
_KILL = "pkill -f '^stress-ng' >/dev/null 2>&1; echo KILLED\n"
_TS = re.compile(r"^\[\s*(\d+\.\d+)\]")
# stress-ng --metrics-brief: "stress-ng: metrc: [pid] cpu  123456  600.01  ...  bogo/s"
_METRIC = re.compile(r"stress-ng:\s+(?:info|metrc):\s+\[\d+\]\s+([a-z][\w-]*)\s+(\d+)\s+"
                     r"([\d.]+)\s+[\d.]+\s+[\d.]+\s+([\d.]+)")


def dmesg_delta(text: str, t0: float) -> list[str]:
    """Kernel log lines stamped at or after t0 (seconds since boot); an unstamped line
    goes with the one before it."""
    out, keep = [], False
    for ln in (text or "").splitlines():
        m = _TS.match(ln)
        if m:
            keep = float(m.group(1)) >= t0
        if keep:
            out.append(ln)
    return out


def dmesg_bad(lines: list[str]) -> list[str]:
    return [ln for ln in lines if any(p in ln for p in DMESG_BAD)]


def new_sel(before: str, after: str) -> str:
    """The SEL lines of `after` not in `before` (by their text)."""
    seen = {ln.strip() for ln in (before or "").splitlines()}
    return "".join(ln + "\n" for ln in (after or "").splitlines()
                   if "|" in ln and ln.strip() not in seen)


def metrics(out: str) -> list[list[str]]:
    return [[f"stress-ng {m.group(1)}", f"{m.group(2)} bogo ops in {m.group(3)} s "
                                        f"({m.group(4)}/s)"]
            for m in _METRIC.finditer(out or "")]


def _split(out: str) -> dict[str, str]:
    secs, cur = {}, None
    for ln in out.splitlines(keepends=True):
        if ln.startswith(_MARK) and ln.rstrip().endswith("==="):
            cur = ln.strip()[len(_MARK):-3]
            secs[cur] = ""
        elif cur is not None:
            secs[cur] += ln
    return secs


def _par(fn, items) -> dict:
    items = list(items)
    if not items:
        return {}
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        return dict(zip([x.pos for x in items], ex.map(fn, items)))


class _Node:
    def __init__(self, n):
        self.n = n
        self.problems: list[str] = []
        self.unanswered: list[str] = []
        self.rows: list[list[str]] = []
        self.arts: dict[str, str] = {}
        self.t0: float | None = None
        self.started: float | None = None
        self.rc: int | None = None
        self.sel: dict[str, str | None] = {"before": None, "after": None}
        self.detail: dict = {}

    @property
    def pos(self):
        return self.n["pos"]


class _Run:
    def __init__(self, nodes, user, pw, bmc_creds, seconds, vm, deadline_s, runner, sleep,
                 clock, poll_s, cancelled):
        self.st = [_Node(n) for n in nodes]
        self.user, self.pw, self.bmc_creds = user, pw, bmc_creds
        self.seconds, self.vm, self.deadline_s = seconds, vm, deadline_s
        self.runner, self.sleep, self.clock, self.poll_s = runner, sleep, clock, poll_s
        self.cancelled = cancelled

    def ssh(self, x, script, timeout=60):
        return hostssh.run(x.n["host_ip"], self.user, self.pw, script, timeout=timeout,
                           runner=self.runner)

    def oob(self, x, when):
        arts, rows, _who, why = endsummary.read_oob(x.n["bmc_ip"], self.bmc_creds,
                                                    runner=self.runner)
        for name, text in arts.items():
            x.arts[f"{name.removesuffix('.txt')}-{when}.txt"] = text
        x.sel[when] = arts.get("sel-elist.txt")
        if x.sel[when] is None:
            x.unanswered.append(f"SEL {when} not read: {why}")
        x.rows += [[f"{k} ({when})", v] for k, v in rows]

    def prep(self, x):
        self.oob(x, "before")
        rc, out, err = self.ssh(x, _PREP, timeout=300)
        if unreachable(rc) or "T0 " not in out:
            x.unanswered.append(f"{x.n['host_ip']} did not answer ssh before stress "
                                f"(rc={rc}) {hostssh.tail(out, err, 120)}".rstrip())
            return
        if "NOSTRESS" in out:
            x.problems.append("stress-ng not installed (apt-get install stress-ng failed)")
            return
        m = re.search(r"^T0 ([\d.]+)", out, re.M)
        x.t0 = float(m.group(1)) if m else None

    def launch(self, xs):
        barrier = threading.Barrier(len(xs))
        cmd = command(self.seconds, self.vm)

        def one(x):
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                pass
            x.started = self.clock()
            rc, out, err = self.ssh(x, _launch(cmd))
            if "STARTED" not in out:
                x.started = None
                x.problems.append(f"stress-ng launch failed (rc={rc}) "
                                  f"{hostssh.tail(out, err, 120)}".rstrip())
        _par(one, xs)

    def wait(self, xs, t_launch) -> bool:
        """-> False when Cancel stopped the run (stress-ng killed on every node)."""
        while (left := self.seconds - (self.clock() - t_launch)) > 0:
            if self.cancelled():
                _par(lambda x: self.ssh(x, _KILL), xs)
                for x in xs:
                    x.unanswered.append("cancelled: stress-ng stopped "
                                        f"{self.clock() - t_launch:.0f} s into its run")
                return False
            self.sleep(min(self.poll_s, left))
        pending = list(xs)
        while pending:
            res = _par(lambda x: self.ssh(x, _DONE), pending)
            pending = [x for x in pending if "DONE" not in res[x.pos][1]]
            if not pending or self.clock() - t_launch >= self.deadline_s:
                break
            self.sleep(self.poll_s)
        for x in pending:       # no exit code by the deadline: stop it
            k = self.ssh(x, _KILL)
            x.problems.append(f"stress-ng gave no exit code within the {self.deadline_s} s "
                              "deadline" + ("; killed" if "KILLED" in k[1] else
                                            "; kill not confirmed"))
        return True

    def collect(self, x):
        rc, out, err = self.ssh(x, _COLLECT, timeout=120)
        secs = _split(out)
        if unreachable(rc) or "rc" not in secs:
            x.problems.append(f"{x.n['host_ip']} did not answer ssh after stress "
                              f"(rc={rc}) {hostssh.tail(out, err, 120)}".rstrip())
            return
        sout = secs.get("out", "")
        x.arts["stress-ng.txt"] = sout
        rcs = secs.get("rc", "").strip()
        x.rc = int(rcs) if rcs.lstrip("-").isdigit() else None
        if x.rc is None:
            if not any("no exit code" in p for p in x.problems):
                x.problems.append("stress-ng left no exit code")
        elif x.rc != 0:
            x.problems.append(f"stress-ng rc={x.rc}: {hostssh.tail(sout, '', 160)}")
        delta = dmesg_delta(secs.get("dmesg", ""), x.t0 or 0.0)
        bad = dmesg_bad(delta)
        x.arts["dmesg-delta.txt"] = "".join(ln + "\n" for ln in delta)
        x.detail["dmesg_bad"] = bad
        x.rows.append(["dmesg during stress", f"{len(delta)} line(s), {len(bad)} MCE/EDAC/"
                                              "thermal"])
        x.rows += [["dmesg", ln] for ln in bad[:10]]
        if bad:
            x.problems.append(f"dmesg: {bad[0].strip()}")
        x.rows += metrics(sout)

    def judge_sel(self, x):
        b, a = x.sel["before"], x.sel["after"]
        if b is None or a is None:
            return
        new = new_sel(b, a)
        bad, tr = endsummary.nonbenign(new), endsummary.transients(new)
        n = sum("|" in ln for ln in new.splitlines())
        x.detail.update(sel_new=n, sel_nonbenign=bad, sel_transients=tr)
        x.rows.append(["SEL during stress", f"{n} new, {len(bad)} non-benign"
                                            + (f", {len(tr)} transient" if tr else "")])
        x.rows += [[f"SEL transient {t['sensor']}", f"{t['event']}: asserted, deasserted "
                                                     f"after {t['seconds']} s"] for t in tr]
        x.rows += [["SEL new", ln] for ln in bad[:10]]
        if bad:
            x.problems.append(f"SEL: {len(bad)} new non-benign: {bad[0].strip()}")

    def result(self, x, spread) -> RungResult:
        det = {**x.detail, "command": command(self.seconds, self.vm), "rc": x.rc,
               "launch_spread_s": spread,
               "rows": [["stress-ng", command(self.seconds, self.vm)
                         + ("" if x.rc is None else f" -> rc {x.rc}")]] + x.rows,
               "notes": [f"stress-ng launched on every node within {spread:.1f} s"
                         if spread is not None else "stress-ng not launched",
                         "fail: stress-ng rc != 0, a dmesg MCE/EDAC/thermal line, no ssh "
                         "after, or a new non-benign SEL entry (60 s threshold transients "
                         "are benign)"]}
        if x.problems:
            why = "; ".join(x.problems)
            return RungResult("fail", {**det, "headline": x.problems[0], "error": why},
                              x.arts)
        if x.unanswered:
            why = "; ".join(x.unanswered)
            return RungResult(UNMEASURED, {**det, "headline": x.unanswered[0], "reason": why,
                                           "error": why}, x.arts)
        return RungResult("pass", {**det, "headline": f"stress-ng {self.seconds} s rc 0 · SEL "
                                   f"{x.detail.get('sel_new', 0)} new, clean · dmesg clean"},
                          x.arts)


def run(nodes: list[dict], *, user: str, pw: str, bmc_creds, seconds: int = SECONDS,
        vm: int = VM_WORKERS, deadline_s: int = DEADLINE_S, runner=subprocess.run,
        sleep=time.sleep, clock=time.monotonic, poll_s: int = POLL_S,
        cancelled=lambda: False) -> dict[str, RungResult]:
    """nodes: [{"pos", "host_ip", "bmc_ip"}]. One RungResult per pos. cancelled(): the
    stage's Cancel, checked every poll_s of the run."""
    R = _Run(nodes, user, pw, bmc_creds, seconds, vm, deadline_s, runner, sleep, clock, poll_s,
             cancelled)
    _par(R.prep, R.st)
    ready = [x for x in R.st if x.t0 is not None and not x.problems]
    spread = None
    if ready:
        R.launch(ready)
        went = [x for x in ready if x.started is not None]
        if went:
            spread = max(x.started for x in went) - min(x.started for x in went)
            if R.wait(went, min(x.started for x in went)):
                _par(R.collect, went)
    _par(lambda x: (R.oob(x, "after"), R.judge_sel(x)), [x for x in R.st if x.t0 is not None])
    return {x.pos: R.result(x, spread) for x in R.st}
