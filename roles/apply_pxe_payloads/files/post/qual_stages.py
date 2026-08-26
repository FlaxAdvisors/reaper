#!/usr/bin/env python3
"""Stage command table + pure digest/calc helpers for the post qualification agent.

Reuses the exact captures the current post.sh/donum flow uses (dmidecode, hwinfo,
ipmitool sdr/sel elist over local KCS, stress with the burn_cpu_memory.yml math,
bundled macinv/dimmerr). Every external command goes through the injectable RUNNER
seam so tests never shell out. Stdlib only; imports under Python 3.6.
"""
import subprocess

AGENT_VER = "1"

HWINFO_FLAGS = ("--arch --bios --block --bridge --cdrom --cpu --disk --framebuffer "
                "--gfxcard --hub --ide --keyboard --memory --mmc-ctrl --monitor --mouse "
                "--netcard --network --partition --pci --pcmcia --pcmcia-ctrl --scsi "
                "--smp --storage-ctrl --sys --tape --tv --uml --usb --usb-ctrl --vbe "
                "--wlan --xen --zip").split()


def _default_runner(argv, timeout):
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "%s not found" % argv[0]
    except OSError as e:
        # e.g. Exec format error (ENOEXEC: a shebang-less script) or permission
        # denied. Degrade to a non-zero rc + message so one malformed bundled tool
        # can't crash a whole stage (a dead ./dimmsum must not sink inventory).
        return 126, "%s: %s" % (argv[0], e)


RUNNER = _default_runner


def sel_digest(sel_elist_text):
    """Collapse `ipmitool sel elist` to unique event -> count, dropping the
    per-record timestamp so identical events logged at different times collapse.
    Real format (matches flax_post observe _parse_sel):
    `id | MM/DD/YYYY | HH:MM:SS | sensor | description` -> event = fields[3:].
    caterr = any line mentions CATERR."""
    counts = {}
    caterr = False
    for line in sel_elist_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        event = " | ".join(parts[3:]) if len(parts) >= 4 else line
        counts[event] = counts.get(event, 0) + 1
        if "CATERR" in line.upper():
            caterr = True
    digest = "\n".join("%d %s" % (n, m) for m, n in sorted(counts.items()))
    return digest, {"unique_msgs": len(counts), "caterr": caterr}


def sel_recent(sel_elist_text, n=50):
    """The most recent n SEL entries in time order (raw lines, timestamps kept).
    `ipmitool sel elist` is chronological ascending, so recent = the tail."""
    lines = [ln.strip() for ln in sel_elist_text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def _lscpu_int(lscpu_text, label):
    for line in lscpu_text.splitlines():
        if label in line:
            for tok in line.split():
                if tok.isdigit():
                    return int(tok)
    return 0


def _meminfo_kb(meminfo_text, label):
    for line in meminfo_text.splitlines():
        if line.startswith(label):
            return int(line.split()[1])
    return 0


def stress_cmd(lscpu_text, meminfo_text, duration, mem_headroom_pct=10):
    """`stress --cpu N -m M --vm-bytes XM --timeout D`, N/M/X from the
    burn_cpu_memory.yml math (total = socket*core*thread; cpu=total/2; mem=cpu-1;
    per-worker MB over MemTotal minus the live-ISO footprint + headroom)."""
    sockets = _lscpu_int(lscpu_text, "Socket")
    cores = _lscpu_int(lscpu_text, "per socket")
    threads = _lscpu_int(lscpu_text, "Thread")
    total = max(sockets * cores * threads, 2)
    cpu_threads = total // 2
    mem_threads = max(cpu_threads - 1, 1)
    total_kb = _meminfo_kb(meminfo_text, "MemTotal:")
    avail_kb = _meminfo_kb(meminfo_text, "MemAvailable:")
    iso_used = total_kb - avail_kb
    reserve = iso_used * (100 + mem_headroom_pct) // 100
    per_worker_mb = (total_kb - reserve) // 1024 // mem_threads
    return "stress --cpu %d -m %d --vm-bytes %dM --timeout %d" % (
        cpu_threads, mem_threads, per_worker_mb, duration)


def edac_totals(dimmerr_text):
    """Sum ce:/ue: across dimmerr's per-DIMM lines."""
    ce = ue = dimms = 0
    for line in dimmerr_text.splitlines():
        if "ce:" not in line or "ue:" not in line:
            continue
        dimms += 1
        toks = line.replace(":", " ").split()
        try:
            ce += int(toks[toks.index("ce") + 1])
            ue += int(toks[toks.index("ue") + 1])
        except (ValueError, IndexError):
            pass
    return {"ce": ce, "ue": ue, "dimms": dimms}
