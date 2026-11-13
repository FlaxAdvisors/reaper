"""The fio drive reference guide: /guides/fio-drives.html at each primary bang.

One row per drive key (model, size): its history (the mined baseline file) and what this site
has collected in post_fio_drive (drives, runs, the latest run per serial). A key the site has
collected but the baseline lacks is "collected only": it joins the history at the next re-mining
(scripts/fio-baseline-mine --drives).  Rendered hourly by flax-post-fio-guide.timer:
    python -m flax_post.fio_guide --out /guides/fio-drives.html"""
import argparse
import datetime
import html
import os
import statistics
import sys
import tempfile

from . import fio_judge as fj

_ORDER = [(job, metric, good) for job in ("bw", "iops", "lat") for metric, good in fj.METRICS[job]]
_HEAD = {("bw", "mbps"): "read MB/s", ("iops", "iops"): "IOPS",
         ("lat", "lat_us"): "latency µs", ("lat", "p99_us"): "p99 µs"}


def _history(baseline, model, size, job, metric):
    """The any-controller history group for a key (a size-less group only for an uncut key)."""
    for g in baseline.get("groups") or []:
        if (g.get("model"), g.get("job"), g.get("metric"), g.get("controller")) != (model, job, metric, None):
            continue
        if "size_gb" in g:
            if fj.size_key(g.get("size_gb")) == size:
                return g
        elif not (model and len(model) == 16):
            return g
    return None


def _outside(v, g, good):
    se = fj.sigma_eff(g["mean"], g["std"])
    return v < g["mean"] - fj.FLAG_SIGMA * se if good == "higher" else v > g["mean"] + fj.FLAG_SIGMA * se


def summarize(baseline, rows) -> list:
    """[{model, size_gb, status, controllers, drives, runs, nodes, last, metrics}] where
    metrics[(job, metric)] = {history, used, latest_n, latest_median, outside}."""
    baseline = baseline or {}
    if baseline.get("suite") not in (None, fj.SUITE):
        baseline = {}
    keys = {}
    for g in baseline.get("groups") or []:
        if g.get("model") and g.get("controller") is None:
            keys.setdefault((g["model"], fj.size_key(g.get("size_gb"))), [])
    for r in rows:
        if r.get("model"):
            keys.setdefault((r["model"], fj.size_key(r.get("size_gb"))), []).append(r)
    out = []
    for (model, size), rs in keys.items():
        latest = {}  # (serial, job, metric) -> row of the newest run
        for r in rs:
            if r.get("serial"):
                k = (r["serial"], r["job"], r["metric"])
                if k not in latest or (r["captured_at"], r["run_id"]) > (latest[k]["captured_at"], latest[k]["run_id"]):
                    latest[k] = r
        metrics, has_hist = {}, False
        for job, metric, good in _ORDER:
            g = _history(baseline, model, size, job, metric)
            has_hist |= g is not None
            used = bool(g) and g.get("n", 0) >= fj.MIN_SAMPLES and not fj.too_wide(g)
            vals = [r["value"] for (s, j, m), r in latest.items() if (j, m) == (job, metric)]
            metrics[(job, metric)] = {
                "history": g, "used": used, "latest_n": len(vals),
                "latest_median": statistics.median(vals) if vals else None,
                "outside": sum(_outside(v, g, good) for v in vals) if used else None}
        status = ("history" if rs else "history only") if has_hist else "collected only"
        out.append({"model": model, "size_gb": size, "status": status,
                    "controllers": sorted({r["controller"] for r in rs if r.get("controller")}),
                    "drives": len({r["serial"] for r in rs if r.get("serial")}),
                    "runs": len({(r["bmc_mac"], r["run_id"]) for r in rs}),
                    "nodes": len({r["bmc_mac"] for r in rs}),
                    "last": max((str(r["captured_at"])[:10] for r in rs), default=None),
                    "metrics": metrics})
    rank = {"collected only": 0, "history": 1, "history only": 2}
    out.sort(key=lambda e: (rank[e["status"]], e["model"], e["size_gb"] or 0))
    return out


def _num(v):
    if v is None:
        return ""
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.3g}"


def _size(v):
    if v is None:
        return "?"
    return f"{v / 1000:g} TB" if v >= 1000 else f"{v:g} GB"


_CSS = """
:root{--bg:#fff;--fg:#1b1f24;--mut:#5b636e;--line:#d8dde3;--head:#f3f5f7;--warn:#9a5b00;--warnbg:#fff4e0;--new:#0b5cad}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#15181c;--fg:#e4e7eb;--mut:#9aa3ad;--line:#30363d;--head:#1e2328;--warn:#f0b35a;--warnbg:#3a2a10;--new:#79b8ff}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 auto;padding:16px;max-width:1500px}
h1{font-size:20px;margin:0 0 4px}p,li{color:var(--mut)}.wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top;white-space:nowrap}
th{background:var(--head);position:sticky;top:0}td.n{text-align:right}
.sub{color:var(--mut);font-size:12px}.out{color:var(--warn);background:var(--warnbg);font-weight:600}
.st-collected{color:var(--new);font-weight:600}.unused{color:var(--mut);text-decoration:line-through}
"""


def _cell(m, good):
    g = m["history"]
    hist = ""
    if g:
        cls = "" if m["used"] else ' class="unused"'
        why = "" if m["used"] else (" too wide" if fj.too_wide(g) else f" n<{fj.MIN_SAMPLES}")
        slow = f", {g.get('slow_dropped')} slow dropped" if g.get("slow_dropped") else ""
        hist = (f'<span{cls}>{_num(g["mean"])} ± {_num(g["std"])}</span>'
                f'<div class="sub">n {g["n"]}{slow}{why}</div>')
    site = ""
    if m["latest_n"]:
        o = m["outside"]
        flag = f' · <span class="out">{o} outside</span>' if o else ""
        site = f'<div class="sub">site median {_num(m["latest_median"])} of {m["latest_n"]}{flag}</div>'
    return f"<td>{hist}{site}</td>"


def render(baseline, rows, site, generated) -> str:
    e = html.escape
    baseline = baseline or {}
    entries = summarize(baseline, rows)
    head = "".join(f"<th>{_HEAD[(j, m)]}</th>" for j, m, _ in _ORDER)
    body = []
    for x in entries:
        st = f'<span class="st-collected">{x["status"]}</span>' if x["status"] == "collected only" else x["status"]
        seen = (f'{x["drives"]} drives · {x["runs"]} runs · {x["nodes"]} nodes'
                f'<div class="sub">last {x["last"]}</div>') if x["runs"] else ""
        body.append(f'<tr><td>{e(x["model"])}<div class="sub">{e(", ".join(x["controllers"]))}</div></td>'
                    f'<td class="n">{_size(x["size_gb"])}</td><td>{st}</td><td>{seen}</td>'
                    + "".join(_cell(x["metrics"][(j, m)], g) for j, m, g in _ORDER) + "</tr>")
    n_col = sum(x["status"] == "collected only" for x in entries)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>fio drive reference</title><style>{_CSS}</style></head><body>
<h1>fio drive reference · {e(site)}</h1>
<p>Generated {e(generated)} from this site's collected drives (post_fio_drive) and the mined history
(baseline {e(str(baseline.get("generated", "none")))}, {e(str(baseline.get("source", "")))}, suite {e(str(baseline.get("suite", fj.SUITE)))}).
{len(entries)} drive keys, {n_col} collected only.</p>
<ul>
<li>A drive key is model + size; the size comes from the inventory capacity (alldisks), never the model name.</li>
<li>History: mean ± σ of the any-controller group after the slow-run drop and the {fj.CLIP_SIGMA:g}σ clip; a drive is
flagged at {fj.FLAG_SIGMA:g}σ on the bad side. Struck out = not used (fewer than {fj.MIN_SAMPLES} samples, or σ/mean
above {fj.SPREAD_CAP * 100:g}%).</li>
<li>Siblings: same key in the node and across the site, at least {fj.MIN_SIBLINGS} drives, flagged when more than
{fj.SIBLING_PCT}% worse than their median.</li>
<li>Site median: the latest run of each serial here; "outside" counts those beyond the history bound. Flags are labels;
they never fail a node.</li>
<li><b>collected only</b>: seen here, not yet in the history; it joins at the next re-mining
(<code>scripts/fio-baseline-mine --drives</code>).</li>
</ul>
<div class="wrap"><table><thead><tr><th>model</th><th>size</th><th>status</th><th>seen here</th>{head}</tr></thead>
<tbody>
{chr(10).join(body)}
</tbody></table></div></body></html>
"""


def main(argv=None, rows=None, baseline=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--site", default=os.environ.get("SITE_NAME", ""))
    a = ap.parse_args(argv)
    if rows is None:
        from . import state
        rows = state.fio_drive_rows()
    if baseline is None:
        baseline = fj.load_baseline()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page = render(baseline, rows, a.site, now)
    d = os.path.dirname(os.path.abspath(a.out))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".fio-drives.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(page)
        os.chmod(tmp, 0o644)
        os.replace(tmp, a.out)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"{a.out}: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
