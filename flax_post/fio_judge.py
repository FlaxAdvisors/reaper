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

BASELINE_PATH = Path(__file__).resolve().parent / "fio-baselines-v1.json"

# job -> [(metric, which direction is good)]
METRICS = {
    "bw": [("mbps", "higher")],
    "iops": [("iops", "higher")],
    "lat": [("lat_us", "lower"), ("p99_us", "lower")],
}

_UNITS = {"mbps": "MB/s", "iops": "IOPS", "lat_us": "µs", "p99_us": "µs p99"}


def normalise_model(s):
    if s is None:
        return None
    s = re.sub(r"^\s*ATA\s+", "", str(s), flags=re.I)
    s = re.sub(r"[_\s]+", " ", s)
    return s.strip().upper()


def load_baseline(path=BASELINE_PATH):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _fmt(v):
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.3g}"


def _group(baseline, model, controller, job, metric):
    exact = anyc = None
    for g in baseline.get("groups") or []:
        if (g.get("model") != model or g.get("job") != job
                or g.get("metric") != metric):
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


def _get(parsed_job, metric):
    try:
        v = ((parsed_job or {}).get("dirs") or {}).get("read", {}).get(metric)
    except AttributeError:
        return None
    return v if isinstance(v, (int, float)) else None


def judge(parsed, disks, baseline):
    baseline = baseline or {}
    out = {}
    for dev, jobs in parsed.items():
        info = disks.get(dev) or {}
        model = normalise_model(info.get("model"))
        ctrl = info.get("controller")
        reasons, expected, judged = [], {}, False
        for job, mlist in METRICS.items():
            pj = (jobs or {}).get(job)
            if not pj:
                continue
            for metric, good in mlist:
                val = _get(pj, metric)
                if val is None:
                    continue
                g = _group(baseline, model, ctrl, job, metric) if model else None
                if g:
                    judged = True
                    mean, std = g["mean"], g.get("std", 0.0)
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
                    if d2 == dev or normalise_model((disks.get(d2) or {}).get("model")) != model:
                        continue
                    v2 = _get((j2 or {}).get(job), metric)
                    if v2 is not None:
                        sib.append(v2)
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
        }
    return out


def flagged(results):
    return [d for d, r in results.items() if r.get("verdict") == "outside"]
