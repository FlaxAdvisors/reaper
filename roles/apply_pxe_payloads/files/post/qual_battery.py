#!/usr/bin/env python3
"""The autonomous qualification battery: runs node stages in order and exposes the
frozen REST-contract snapshots. Stdlib only; imports under Python 3.6.

State machine: running -> done (all pass) | fault (any fail). request_restart()
mints a new run_id and holds in reset_pending (idempotent); ack_restart() clears
and readies the next run() (the server's caller re-launches run()).
"""
import threading
import time
import uuid

import qual_stages

_MAP = {"pass": "pass", "fail": "fail", "skip": "skip"}


class Battery(object):
    def __init__(self, runner=None, stages=None, mac="", serial=""):
        self._runner = runner or qual_stages.RUNNER
        self._stages = stages if stages is not None else DEFAULT_STAGES
        self.mac = mac
        self.serial = serial
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.run_id = uuid.uuid4().hex
        self.state = "running"
        self.verdict = None
        self.current = None
        self._steps = [{"name": s["name"], "status": "pending", "verdict": None,
                        "started": None, "ended": None, "summary": {}, "arts": {}}
                       for s in self._stages]

    def run(self):
        my_run_id = self.run_id
        for i, s in enumerate(self._stages):
            with self._lock:
                if self.state == "reset_pending" or self.run_id != my_run_id:
                    return
                self.current = s["name"]
                self._steps[i]["status"] = "running"
                self._steps[i]["started"] = int(time.time())
            try:
                res = s["fn"](self._runner)
            except Exception as e:                       # a stage must not crash the run
                res = {"verdict": "fail", "summary": {"error": str(e)}, "artifacts": {}}
            with self._lock:
                if self.run_id != my_run_id:             # a restart ack superseded this run mid-stage
                    return
                st = self._steps[i]
                st["verdict"] = res.get("verdict", "fail")
                st["status"] = _MAP.get(st["verdict"], "fail")
                st["summary"] = res.get("summary", {})
                st["arts"] = res.get("artifacts", {})
                st["ended"] = int(time.time())
        with self._lock:
            if self.run_id != my_run_id:
                return
            self.current = None
            self.verdict = "pass" if all(s["status"] in ("pass", "skip")
                                         for s in self._steps) else "fail"
            self.state = "done" if self.verdict == "pass" else "fault"

    # --- contract snapshots ---
    def health(self):
        with self._lock:
            return {"ok": True, "mac": self.mac, "serial": self.serial,
                    "run_id": self.run_id, "state": self.state, "agent_ver": qual_stages.AGENT_VER}

    def status(self):
        with self._lock:
            done = sum(1 for s in self._steps if s["status"] in ("pass", "fail", "skip"))
            total = len(self._steps)
            st = "fault" if any(s["status"] == "fail" for s in self._steps) else \
                 ("done" if done == total else "running")
            return {"status": st, "current": self.current, "done_n": done, "total_n": total,
                    "verdict": self.verdict, "pct": int(done * 100 / total) if total else 0}

    def stages_list(self):
        with self._lock:
            return [{"name": s["name"], "status": s["status"], "verdict": s["verdict"],
                     "started": s["started"], "ended": s["ended"]} for s in self._steps]

    def stage(self, name):
        with self._lock:
            for s in self._steps:
                if s["name"] == name:
                    arts = [{"name": n, "kind": kind, "bytes": len(text)}
                            for n, (kind, text) in s["arts"].items()]
                    return {"name": name, "status": s["status"], "verdict": s["verdict"],
                            "started": s["started"], "ended": s["ended"],
                            "summary": s["summary"], "artifacts": arts}
            return None

    def artifact(self, name, art):
        with self._lock:
            for s in self._steps:
                if s["name"] == name and art in s["arts"]:
                    return s["arts"][art][1]
            return None

    def request_restart(self):
        with self._lock:
            if self.state != "reset_pending":
                self._pending_old = self.run_id
                self._pending_new = uuid.uuid4().hex
                self.state = "reset_pending"
            return {"old_run_id": self._pending_old, "new_run_id": self._pending_new}

    def ack_restart(self):
        with self._lock:
            self._reset()
            self.run_id = self._pending_new
        return {"ok": True}


# --- the real node battery (stage fns live in qual_stages via closures) ---
from qual_stages import HWINFO_FLAGS, sel_digest, sel_recent, stress_cmd, edac_totals   # noqa: E402


def _cap(runner, argv, timeout=60):
    return runner(argv, timeout)[1]


def _sdr(runner):
    out = _cap(runner, ["ipmitool", "sdr", "elist"])
    return {"verdict": "pass", "summary": {"lines": out.count("\n")},
            "artifacts": {"sdr": ("raw", out)}}


def _sel(runner):
    raw = _cap(runner, ["ipmitool", "sel", "elist"])
    digest, summary = sel_digest(raw)
    return {"verdict": "fail" if summary["caterr"] else "pass", "summary": summary,
            "artifacts": {"sel-raw": ("raw", raw), "sel-digest": ("digest", digest),
                          "sel-recent": ("digest", sel_recent(raw))}}


def _sel_clear(runner):
    rc, out = runner(["ipmitool", "sel", "clear"], 30)
    return {"verdict": "pass" if rc == 0 else "fail", "summary": {}, "artifacts": {}}


def _tooling(runner):
    runner(["zypper", "addrepo", "-fG",
            "https://download.opensuse.org/repositories/server:monitoring/openSUSE_Tumbleweed/server:monitoring.repo"], 120)
    runner(["zypper", "--non-interactive", "--no-gpg-checks", "--no-refresh", "install", "stress"], 300)
    runner(["zypper", "--non-interactive", "--no-gpg-checks", "install", "iperf", "fio"], 300)  # openSUSE pkg 'iperf' -> iperf3 binary
    rc, out = runner(["sh", "-c", "command -v stress && command -v iperf3 && command -v fio"], 15)
    return {"verdict": "pass" if rc == 0 else "fail", "summary": {}, "artifacts": {"tooling": ("raw", out)}}


# (artifact-name, argv, kind) -- the post.sh inventory collection set, reused verbatim.
_INV_CMDS = [
    ("dmidecode", ["dmidecode"], "raw"), ("hwinfo", ["hwinfo"] + HWINFO_FLAGS, "raw"),
    ("lscpu", ["lscpu"], "raw"), ("lspci-vv", ["lspci", "-vv"], "raw"),
    ("lsblk", ["lsblk"], "raw"), ("blkid", ["blkid"], "raw"),
    ("lsscsi-c", ["lsscsi", "-c"], "raw"), ("lsscsi-g", ["lsscsi", "-g"], "raw"),
    ("lsusb-v", ["lsusb", "-v"], "raw"),
    ("ip-d-link", ["ip", "-d", "link"], "raw"), ("ip-d-address", ["ip", "-d", "address"], "raw"),
    ("lldpcli-neigh", ["lldpcli", "show", "neigh"], "raw"),
    ("cpuinfo", ["cat", "/proc/cpuinfo"], "raw"), ("meminfo", ["cat", "/proc/meminfo"], "raw"),
    ("ipmitool-fru", ["ipmitool", "fru"], "raw"),
    ("ipmitool-mc-info", ["ipmitool", "mc", "info"], "raw"),
    ("ipmitool-sensor", ["ipmitool", "sensor", "list", "all"], "raw"),
    ("dimmsum", ["./dimmsum"], "raw"), ("lsnet", ["./lsnet"], "raw"),   # bundled ghost bins
    ("alldisks", ["./alldisks", "-v"], "raw"), ("bootorder", ["./bootorder"], "raw"),
]


def _smartctl_all(runner):
    scan = _cap(runner, ["smartctl", "--scan"])
    out = []
    for line in scan.splitlines():
        toks = line.split()
        if toks:
            out.append(_cap(runner, ["smartctl", "--all", toks[0]]))
    return "\n".join(out)


# macinv is a SERVER-side tool: it parses collected inventory FILES in a
# post-<mac>/latest/ dir (these exact names), NOT live hardware. Materialize that
# layout from live captures in a scratch dir, then run `macinv -p` to emit the
# population count-form the engine's population-check evaluates. `macinv -p .` (the
# old call) was wrong -- cwd holds no post-<mac> tree. Validated live on
# fl001-et10b4 2026-07-15. macinv (and any helper it shells, e.g. macformat) lives in
# /opt/flax/bin, which the agent's systemd-run unit does NOT put on PATH (its PATH is
# just /usr/{local/,}{s,}bin) -- so the script prepends it, else `macinv` is
# command-not-found and the dump is silently garbage. hwinfo here is a fast subset
# (macinv only reads its Ethernet controllers); the full hwinfo is a separate artifact.
_MACINV_SH = r"""set -e
export PATH="/opt/flax/bin:$PATH"
mac=$(sed -rn 's/.*BOOTIF=01-([0-9A-Fa-f-]+).*/\1/p' /proc/cmdline | tr -d - | tr 'A-F' 'a-f')
[ -n "$mac" ] || mac=000000000000
d=$(mktemp -d)/post-$mac; mkdir -p "$d/inv"
dmidecode                                                > "$d/inv/dmidecode.txt"            2>&1 || true
hwinfo --bios --cpu --memory --netcard --network --pci   > "$d/inv/hwinfo.txt"               2>&1 || true
lspci -vvv                                               > "$d/inv/lspci-vvv.txt"            2>&1 || true
ipmitool fru                                             > "$d/inv/ipmitool_fru.txt"         2>&1 || true
ipmitool lan print 1                                     > "$d/inv/ipmitool_lan_print_1.txt" 2>&1 || true
ipmitool lan print 8                                     > "$d/inv/ipmitool_lan_print_8.txt" 2>&1 || true
ipmitool mc info                                         > "$d/inv/ipmitool_mc_info.txt"     2>&1 || true
lldpcli show neigh                                       > "$d/inv/lldpcli-show-neigh.txt"   2>&1 || true
for i in /sys/class/net/*; do n=${i##*/}; [ "$n" = lo ] && continue; ethtool -i "$n" > "$d/inv/ethtool-i_$n.txt" 2>&1 || true; done
ln -sfn inv "$d/latest"
macinv -p "$d"
"""


def _macinv_population(runner):
    return _cap(runner, ["bash", "-c", _MACINV_SH], 180)


def _inventory(runner):
    arts = {}
    for name, argv, kind in _INV_CMDS:
        arts[name] = (kind, _cap(runner, argv, 120))
    arts["smartctl-all"] = ("raw", _smartctl_all(runner))
    _cap(runner, ["./collect_mellanox.sh", "."], 120)               # writes mstflint-d_*_query.txt
    arts["macinv"] = ("digest", _macinv_population(runner))         # population count-form (design §5.6)
    return {"verdict": "pass", "summary": {"tools": len(arts)}, "artifacts": arts}


def _fio(runner):
    # RO fio on NON-mounted block devices (nvme*/sd*); bw/lat/iops jobs (fio_run_ro_tests.yml).
    import re
    lsblk = _cap(runner, ["lsblk", "-ndo", "NAME,MOUNTPOINT"])
    devs = []
    for line in lsblk.splitlines():
        f = line.split()
        if f and re.match(r"^(nvme\d+n\d+|sd[a-z]+)$", f[0]) and len(f) < 2:  # no mountpoint col
            devs.append(f[0])
    if not devs:
        # diskless node (RAM live-overlay, no physical media) -> skip, not fail
        return {"verdict": "skip", "summary": {"reason": "no physical storage media"}, "artifacts": {}}
    runner(["sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"], 30)
    common = ("--numjobs=4 --runtime=30 --time_based --direct=1 --ioengine=libaio "
              "--group_reporting --readonly")
    jobs = {"bw": "--bs=64k --iodepth=64 --rw=read --name=raw-read",
            "lat": "--bs=4k --iodepth=1 --rw=randread --name=readlatency",
            "iops": "--bs=4k --iodepth=256 --rw=read --name=iops"}
    out, ok = [], True
    for dev in devs:
        for jn, jargs in jobs.items():
            rc, o = runner(["sh", "-c", "/usr/bin/fio %s %s --filename /dev/%s" % (common, jargs, dev)], 120)
            out.append("== %s %s ==\n%s" % (dev, jn, o)); ok = ok and rc == 0
    return {"verdict": "pass" if ok else "fail", "summary": {"devices": devs},
            "artifacts": {"fio": ("raw", "\n".join(out))}}


def _iperf(runner):
    # A single iperf3 server on THIS SITE's backup bang IS the serializer (iperf3 serves
    # one test at a time; a busy client retries with backoff until it wins a slot). The
    # host is FLAX_IPERF_SERVER, rendered per-site into flax-qual.env by apply_pxe_payloads
    # (eindhoven=bang-edam, braintree=bang-siesta); the bang-edam default is the fallback.
    import os, time
    server = os.environ.get("FLAX_IPERF_SERVER", "bang-edam")
    backoff = int(os.environ.get("FLAX_IPERF_BACKOFF", "10"))
    deadline = time.time() + int(os.environ.get("FLAX_IPERF_MAX_WAIT", "1800"))
    attempt = 0
    while True:
        attempt += 1
        rc, out = runner(["iperf3", "-c", server, "-i5", "-p", "11001", "-t30"], 90)
        if rc == 0:
            return {"verdict": "pass", "summary": {"server": server, "attempts": attempt},
                    "artifacts": {"iperf": ("raw", out)}}
        if "busy" not in out.lower() or time.time() > deadline:
            return {"verdict": "fail", "summary": {"server": server, "attempts": attempt},
                    "artifacts": {"iperf": ("raw", out)}}
        time.sleep(backoff)   # server busy with another node -> queue


def _mem(runner):
    out = _cap(runner, ["./dimmerr"])                       # bundled EDAC reader
    return {"verdict": "pass", "summary": edac_totals(out), "artifacts": {"edac": ("raw", out)}}


def _stress(runner):
    import os
    dur = int(os.environ.get("FLAX_STRESS_SECONDS", "600"))
    lscpu = _cap(runner, ["lscpu"])
    meminfo = _cap(runner, ["cat", "/proc/meminfo"])
    cmd = stress_cmd(lscpu, meminfo, dur)
    rc, out = runner(["sh", "-c", cmd], dur + 120)
    return {"verdict": "pass" if rc == 0 else "fail", "summary": {"cmd": cmd},
            "artifacts": {"stress": ("raw", out)}}


DEFAULT_STAGES = [
    {"name": "sdr-pre", "fn": _sdr}, {"name": "sel-pre", "fn": _sel},
    {"name": "sel-clear", "fn": _sel_clear}, {"name": "tooling", "fn": _tooling},
    {"name": "inventory", "fn": _inventory}, {"name": "fio", "fn": _fio},
    {"name": "iperf", "fn": _iperf},
    {"name": "mem-pre", "fn": _mem}, {"name": "cpu-mem-stress", "fn": _stress},
    {"name": "mem-post", "fn": _mem}, {"name": "sdr-post", "fn": _sdr},
    {"name": "sel-post", "fn": _sel},
]
