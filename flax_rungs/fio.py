"""The Qualify fio stage's evidence summary: per storage device, per job, IOPS,
bandwidth and latency from the post.tgz agent's `fio` artifact.

The artifact (qual_battery._fio) is fio's default text output per device and job, each
behind a `== <dev> <job> ==` line; the jobs are read-only (--readonly), 4 x 30 s:
  bw    64 KiB sequential read, iodepth 64
  lat   4 KiB random read, iodepth 1
  iops  4 KiB sequential read, iodepth 256
Per job and direction (`read:` / `write:` line, as present): IOPS from `IOPS=40.1k`,
bandwidth in MB/s from the decimal figure in its parentheses (`BW=2506MiB/s (2628MB/s)`),
latency avg from the total `lat (usec|nsec|msec): ... avg=` line, p99 from the
`clat percentiles (<unit>)` 99.00th entry; everything shown in microseconds. Model and
size come from the inventory's alldisks capture (lsblk's SIZE when it has none). A part
that cannot be read says so in its row; the stage's verdict is the agent's, never this."""
import re

JOBS = ("bw", "lat", "iops")
COLS = ["device", "model / size", "job", "IOPS", "MB/s", "lat avg µs", "p99 µs"]
NOTES = ["fio read-only (--readonly) on every unmounted nvme/sd disk: bw = 64 KiB read qd64, "
         "lat = 4 KiB randread qd1, iops = 4 KiB read qd256; 4 jobs x 30 s each",
         "IOPS and MB/s from fio's per-direction line (MB/s decimal); lat avg = total "
         "latency, p99 = completion latency 99.00th percentile"]

_SEC = re.compile(r"^== (\S+) (\S+) ==\s*$", re.M)
_JOBLINE = re.compile(r"^\S+: \(g=\d+\): rw=(\w+), bs=\(R\) ([\d.]+\w*?)-.*?iodepth=(\d+)", re.M)
_DIR = re.compile(r"^\s+(read|write): IOPS=([\d.]+)([kM]?), BW=\S+ \(([\d.]+)([kMG]?)B/s\)", re.M)
_LAT = re.compile(r"^\s+lat \((nsec|usec|msec)\): min=[\d.]+[kM]?, max=[\d.]+[kM]?, "
                  r"avg=([\d.]+)([kM]?)", re.M)
_PCT = re.compile(r"^\s+clat percentiles \((nsec|usec|msec)\):", re.M)
_P99 = re.compile(r"99\.00th=\[\s*(\d+)\]")
_ERR = re.compile(r"\berr=\s*(\d+)")
_US = {"nsec": 1e-3, "usec": 1.0, "msec": 1e3}
_SI = {"": 1, "k": 1e3, "M": 1e6}
_MB = {"": 1e-6, "k": 1e-3, "M": 1.0, "G": 1e3}


def sections(text: str) -> list[tuple[str, str, str]]:
    """[(dev, job, its fio output)] in artifact order."""
    ms = list(_SEC.finditer(text or ""))
    return [(m.group(1), m.group(2), text[m.end():ms[i + 1].start() if i + 1 < len(ms) else None])
            for i, m in enumerate(ms)]


def parse_job(out: str) -> dict:
    """{"rw", "bs", "iodepth", "err", "dirs": {read|write: {"iops", "mbps", "lat_us",
    "p99_us"}}} of one job's text output; missing parts are None / absent."""
    j = _JOBLINE.search(out)
    e = _ERR.search(out)
    res = {"rw": j and j.group(1), "bs": j and j.group(2), "iodepth": j and int(j.group(3)),
           "err": int(e.group(1)) if e else None, "dirs": {}}
    heads = list(_DIR.finditer(out))
    for i, m in enumerate(heads):
        blk = out[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(out)]
        blk = blk.split("\nRun status group", 1)[0]
        lat = _LAT.search(blk)
        p99 = None
        if pc := _PCT.search(blk):
            if pm := _P99.search(blk, pc.end()):
                p99 = int(pm.group(1)) * _US[pc.group(1)]
        res["dirs"][m.group(1)] = {
            "iops": float(m.group(2)) * _SI[m.group(3)],
            "mbps": float(m.group(4)) * _MB[m.group(5)],
            "lat_us": float(lat.group(2)) * _SI[lat.group(3)] * _US[lat.group(1)] if lat else None,
            "p99_us": p99}
    return res


def disks(alldisks: str | None, lsblk: str | None) -> dict[str, str]:
    """dev -> "model · size" from the inventory's alldisks (`/dev/<dev> ... rotational
    <GB> <vendor> <rev> <model> <serial>`), else lsblk's SIZE."""
    out = {}
    for ln in (alldisks or "").splitlines():
        f = ln.split()
        if not f or not f[0].startswith("/dev/"):
            continue
        at = next((i for i, x in enumerate(f) if x.endswith("rotational")), None)
        size = f"{float(f[at + 1]):g} GB" if at is not None and at + 1 < len(f) and \
            re.fullmatch(r"[\d.]+", f[at + 1]) else None
        model = f[-2] if at is not None and len(f) >= at + 5 else None
        out[f[0][5:]] = " · ".join(x for x in (model, size) if x)
    for ln in (lsblk or "").splitlines()[1:]:
        f = ln.split()
        if len(f) >= 6 and f[5] == "disk" and f[0] not in out:
            out[f[0]] = f[3]
    return out


def disk_info(alldisks: str | None) -> dict[str, dict]:
    """dev -> {"model", "controller", "serial", "size_gb"} from alldisks, the judge's
    inputs (flax_post.fio_judge): controller = the driver column before `rotational`
    (mpt3sas / ahci / nvme ...), model = second-to-last field, serial = last; the same
    reading the baseline miner applies to the archive's alldisks."""
    out = {}
    for ln in (alldisks or "").splitlines():
        f = ln.split()
        if not f or not f[0].startswith("/dev/"):
            continue
        at = next((i for i, x in enumerate(f) if x.endswith("rotational")), None)
        full = at is not None and len(f) >= at + 5
        size = float(f[at + 1]) if at is not None and at + 1 < len(f) and \
            re.fullmatch(r"[\d.]+", f[at + 1]) else None
        out[f[0][5:]] = {"model": f[-2] if full else None,
                         "controller": f[at - 1] if at else None,
                         "serial": f[-1] if full else None, "size_gb": size}
    return out


def _n(v, unit=""):
    if v is None:
        return "—"
    if unit == "iops":
        return (f"{v / 1e6:.2f}M" if v >= 1e6 else f"{v / 1e3:.0f}k" if v >= 1e5
                else f"{v / 1e3:.1f}k" if v >= 1e3 else f"{v:.0f}")
    return f"{v:.1f}" if v < 100 else f"{v:.0f}"


def natural_dev_key(dev: str) -> tuple:
    """Sort key: sda < sdb < ... < sdz < sdaa, then nvme0n1 < ... < nvme10n1, others last."""
    if m := re.fullmatch(r"sd([a-z]+)", dev):
        return (0, len(m.group(1)), m.group(1))
    if m := re.fullmatch(r"nvme(\d+)n(\d+)", dev):
        return (1, int(m.group(1)), int(m.group(2)))
    return (2, 0, dev)


def summarize(fio_text: str | None, devices=None, alldisks=None, lsblk=None,
              excluded=None, judged: dict | None = None) -> dict:
    """Detail additions for the fio rung: {"table": {"cols", "rows"}, "fio": {dev: {job:
    parse_job}}, "fio_headline": str | None, "rows", "notes"}. devices: the agent's list
    (summary.devices), so a device with no output is named. excluded: the agent's
    summary.smart_excluded ({dev: reasons}), the disks smart-health failed and fio left out.
    judged: flax_post.fio_judge.judge's {dev: {"verdict", "reasons", "expected"}}; given,
    the table gains `expected` + `check` columns and `row_cls` ("flag" on an outside
    drive's rows), labels only."""
    secs = sections(fio_text or "")
    got: dict[str, dict] = {}
    for dev, job, out in secs:
        got.setdefault(dev, {})[job] = parse_job(out)
    devs = list(dict.fromkeys([*(devices or []), *got]))
    info = disks(alldisks, lsblk)
    missing = []
    by_job: dict[str, list] = {}      # job -> rows, devices in natural order
    gone = []                         # one row per device with no fio output at all
    for dev in sorted(devs, key=natural_dev_key):
        what = info.get(dev) or "—"
        jobs = got.get(dev)
        if not jobs:
            gone.append([dev, what, "—", "no fio output for this device", "", "", ""])
            missing.append(f"{dev}: no output")
            continue
        for job in [*JOBS, *(j for j in jobs if j not in JOBS)]:
            out = by_job.setdefault(job, [])
            r = jobs.get(job)
            if r is None:
                out.append([dev, what, job, "job not in the output", "", "", ""])
                missing.append(f"{dev} {job}: not run")
                continue
            label = job + (f" · {r['rw']} {r['bs']} qd{r['iodepth']}" if r["rw"] else "")
            if not r["dirs"]:
                why = f"fio err={r['err']}" if r["err"] else "no read/write summary line"
                out.append([dev, what, label, f"not parsed: {why}", "", "", ""])
                missing.append(f"{dev} {job}: {why}")
                continue
            for dname, d in r["dirs"].items():
                out.append([dev, what, label + ("" if dname == "read" else f" ({dname})"),
                            _n(d["iops"], "iops"), _n(d["mbps"]), _n(d["lat_us"]),
                            _n(d["p99_us"])])
    rows = [r for job in [*JOBS, *(j for j in by_job if j not in JOBS)]
            for r in by_job.get(job, [])] + gone
    table = {"cols": list(COLS), "rows": rows}
    if judged is not None:
        row_cls = []
        for r in rows:
            j = (judged.get(r[0]) if r[0] in got else None) or {}
            job = r[2].split(" ", 1)[0]
            # check names the job that is out (an outside drive's other jobs read ok);
            # the tint marks every row of the flagged drive
            check = j.get("verdict", "")
            if check == "outside" and not any(x.split(" ", 1)[0] == job
                                               for x in j.get("reasons") or []):
                check = "ok"
            r += [(j.get("expected") or {}).get(job, ""), check]
            row_cls.append("flag" if j.get("verdict") == "outside" else "")
        table = {"cols": [*COLS, "expected", "check"], "rows": rows, "row_cls": row_cls}
    head = None
    lead = next((d for d in devs if d.startswith("nvme") and d in got), None) or \
        next((d for d in devs if d in got), None)
    if lead:
        rd = lambda j: ((got[lead].get(j) or {}).get("dirs") or {}).get("read") or {}
        head = (f"{lead} {_n(rd('iops').get('iops'), 'iops')} IOPS · "
                f"{_n(rd('bw').get('mbps'))} MB/s · {_n(rd('lat').get('lat_us'))} µs"
                + (f" · +{len(devs) - 1} device{'s' if len(devs) > 2 else ''}"
                   if len(devs) > 1 else ""))
    srows = [["devices", ", ".join(f"{d} ({info[d]})" if info.get(d) else d for d in devs)
              or "none"]]
    if missing:
        srows.append(["missing", "; ".join(missing)])
    if excluded:
        srows.append(["left out (SMART failed)", "; ".join(
            f"{d}" + (f" ({info[d]})" if info.get(d) else "") + f": {'; '.join(r or [])}"
            for d, r in excluded.items())])
    return {"table": table, "fio_headline": head,
            "fio": got, "rows": srows, "notes": list(NOTES)}
