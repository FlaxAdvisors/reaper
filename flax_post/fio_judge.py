"""Shared fio judge: label each device's fio results against fleet history
(per model/controller/job/metric) and against same-model siblings.

The judge only labels; out of range never fails anything.  Stdlib only.
It takes already-parsed jobs (flax_rungs.fio.parse_job shape), not raw fio output.
"""
import json
import re
import statistics
from pathlib import Path

SUITE = "v1"
FLAG_SIGMA = 2.0
CLIP_SIGMA = 3.0
CLIP_ROUNDS = 5
MIN_SAMPLES = 5
SIBLING_PCT = 20
MIN_SIBLINGS = 3  # the device + at least two others
SIGMA_FLOOR_FRAC = 0.01  # effective σ = max(std, this × |mean|): a std-0 group never flags on a hair
SPREAD_CAP = 0.15  # a history group with std / |mean| above this is too wide to judge by (kept in the file)

BASELINE_PATH = Path(__file__).resolve().parent / "fio-baselines-v1.json"

# job -> [(metric, which direction is good)]
METRICS = {
    "bw": [("mbps", "higher")],
    "iops": [("iops", "higher")],
    "lat": [("lat_us", "lower"), ("p99_us", "lower")],
}

_UNITS = {"mbps": "MB/s", "iops": "IOPS", "lat_us": "µs", "p99_us": "µs p99"}
SPREAD_LABEL = "no baseline (spread too wide)"


def normalise_model(s, dev=None):
    """The grouping key for a drive model.  Given `dev`, a non-NVMe disk is keyed on
    the 16-character sysfs form that runtime `alldisks -v` shows (smartctl gives the
    full name, e.g. `HGST HUS726060ALE611` vs sysfs `HGST HUS726060AL`)."""
    if s is None:
        return None
    s = re.sub(r"^\s*ATA\s+", "", str(s), flags=re.I)
    s = re.sub(r"[_\s]+", " ", s).strip().upper()
    if dev and not str(dev).lower().startswith("nvme"):
        s = s[:16].strip()
    return s


def size_key(size_gb):
    """A drive's size as a grouping key: 3 significant figures (3840.7 and 3840.8 GB are one
    size); None when unknown (None matches only None)."""
    if size_gb is None:
        return None
    try:
        return float(f"{float(size_gb):.3g}")
    except (TypeError, ValueError):
        return None


def drive_key(info, dev):
    """(model, size): what makes two drives siblings, in a node or across the site."""
    info = info or {}
    return normalise_model(info.get("model"), dev), size_key(info.get("size_gb"))


def _cut(model, dev):
    """A non-NVMe model key cut to the 16-char sysfs form (the cut may drop the size)."""
    return bool(model) and not str(dev or "").lower().startswith("nvme") and len(model) == 16


def load_baseline(path=BASELINE_PATH):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _fmt(v):
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.3g}"


def _group(baseline, model, controller, job, metric, size=None, cut=False):
    """The history group for a drive: a group carrying size_gb matches only that size; a
    group without it (the v1 file) is used only when the model key was not cut."""
    exact = anyc = None
    for g in baseline.get("groups") or []:
        if (g.get("model") != model or g.get("job") != job
                or g.get("metric") != metric):
            continue
        if "size_gb" in g:
            if size_key(g.get("size_gb")) != size:
                continue
        elif cut:
            continue
        if g.get("controller") == controller and controller is not None:
            exact = g
        elif g.get("controller") is None:
            anyc = g
    if exact and exact.get("n", 0) >= MIN_SAMPLES:
        return exact
    if anyc and anyc.get("n", 0) >= MIN_SAMPLES:
        return anyc
    return None


def spread(g):
    """A history group's std / |mean| (inf for a zero mean with any std)."""
    mean, std = abs(g.get("mean") or 0.0), g.get("std") or 0.0
    if mean == 0:
        return 0.0 if std == 0 else float("inf")
    return std / mean


def too_wide(g):
    return spread(g) > SPREAD_CAP


def sigma_eff(mean, std):
    """The σ the history check uses: the group's std, floored at SIGMA_FLOOR_FRAC of
    |mean| (quantised fio output can clip a uniform group down to std 0)."""
    return max(std or 0.0, SIGMA_FLOOR_FRAC * abs(mean))


def _get(parsed_job, metric):
    try:
        v = ((parsed_job or {}).get("dirs") or {}).get("read", {}).get(metric)
    except AttributeError:
        return None
    return v if isinstance(v, (int, float)) else None


def judge(parsed, disks, baseline, peers=None):
    """peers: {(model, size): {(job, metric): [value, ...]}} from same-key drives in other
    nodes at the site; pooled with this node's same-key drives for the sibling check."""
    peers = peers or {}
    baseline = baseline or {}
    if baseline.get("suite") not in (None, SUITE):
        baseline = {}  # another fio job definition's numbers never judge this suite
    out = {}
    for dev, jobs in parsed.items():
        info = disks.get(dev) or {}
        model, size = drive_key(info, dev)
        cut = _cut(model, dev)
        ctrl = info.get("controller")
        reasons, expected, judged, wide = [], {}, False, {}
        for job, mlist in METRICS.items():
            pj = (jobs or {}).get(job)
            if not pj:
                continue
            for metric, good in mlist:
                val = _get(pj, metric)
                if val is None:
                    continue
                g = _group(baseline, model, ctrl, job, metric, size, cut) if model else None
                if g and too_wide(g):
                    # history too spread to judge by: a label, the sibling check still runs
                    wide.setdefault(job, []).append(metric)
                    if metric == mlist[0][0]:
                        expected[job] = SPREAD_LABEL
                    g = None
                if g:
                    judged = True
                    mean = g["mean"]
                    std = sigma_eff(mean, g.get("std", 0.0))
                    if good == "higher":
                        lim = mean - FLAG_SIGMA * std
                        bad = val < lim
                        rng = f"≥ {_fmt(lim)} {_UNITS[metric]}"
                    else:
                        lim = mean + FLAG_SIGMA * std
                        bad = val > lim
                        rng = f"≤ {_fmt(lim)} {_UNITS[metric]}"
                    if metric == mlist[0][0]:
                        expected[job] = rng
                    if bad:
                        reasons.append(f"{job} {metric} {_fmt(val)} vs expected {rng}")
                # sibling check
                if not model:
                    continue
                sib = []
                for d2, j2 in parsed.items():
                    if d2 == dev or drive_key(disks.get(d2), d2) != (model, size):
                        continue
                    v2 = _get((j2 or {}).get(job), metric)
                    if v2 is not None:
                        sib.append(v2)
                sib.extend(v for v in (peers.get((model, size)) or {}).get((job, metric)) or []
                           if isinstance(v, (int, float)))
                if len(sib) + 1 >= MIN_SIBLINGS:
                    judged = True
                    med = statistics.median(sib)
                    if med > 0:
                        worse = (med - val) / med if good == "higher" else (val - med) / med
                        if worse * 100 > SIBLING_PCT:
                            reasons.append(
                                f"{job} {metric} {_fmt(val)} is {worse * 100:.0f}% worse "
                                f"than {len(sib)} sibling median {_fmt(med)}")
        out[dev] = {
            "verdict": ("outside" if reasons else "ok") if judged else "no baseline",
            "reasons": reasons,
            "expected": expected,
            "spread": wide,  # job -> metrics whose history group was set aside (SPREAD_CAP)
        }
    return out


def flagged(results):
    return [d for d, r in results.items() if r.get("verdict") == "outside"]


def no_baseline_fragment(results):
    """The headline fragment naming unjudged drives (a label, never a flag): "" when
    every drive was judged, " · no baseline" when none was, else " · N drive(s) no baseline"."""
    res = results or {}
    n = sum(1 for r in res.values() if (r or {}).get("verdict") == "no baseline")
    if not n:
        return ""
    if n == len(res):
        return " · no baseline"
    return f" · {n} drive{'s' if n != 1 else ''} no baseline"


def note(site: bool = True) -> str:
    """The judge's constants as one sentence for a fio step's notes (both UIs).
    site=False: siblings were only the node's own drives (rackpost judges no site peers)."""
    return (f"expected / check: each drive against its model's fleet history (mean "
            f"{FLAG_SIGMA:g}σ on the bad side, σ ≥ {SIGMA_FLOOR_FRAC * 100:g}% of the mean, "
            f"n ≥ {MIN_SAMPLES}, history "
            f"{CLIP_SIGMA:g}σ-clipped; a history spread σ/mean > {SPREAD_CAP * 100:g}% is "
            f"not used: no baseline (spread too wide)) and against same-model, same-size siblings in the node" + (" and across the site" if site else "") + " "
            f"(> {SIBLING_PCT}% worse than their median, ≥ {MIN_SIBLINGS} "
            f"drives); outside = flagged (a label), never a failure; no baseline = neither check "
            f"applied")
