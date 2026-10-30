"""The process report: internal, every attempt and issue of an order across ALL passes.

The third report beside the two customer CSVs, and the only one for us: what was tried,
what failed and why, so the process can improve. It is never part of the customer
CSVs nor of the Close archive payload. Close writes it before archiving (Close fails,
the order stays open, if it cannot be written); `api/report/process.html` rewrites it
on demand.

    process-report.json   the data
    process-report.html   the same, one section per pass, then per device

Sources: order.db only -- every run (attempt), waiver, event, link_event and pass row --
plus the artifact paths the runs recorded (linked relative to the order dir, never
inlined). Per pass: the attempts per device (stage, rung, verdict, duration, headline),
failure categories with counts, waivers (who / why), interruptions (rungs a daemon
restart cut short, and the restart events), link events (cable pulls = a link that
was up going down, re-plugs = back up after a pull) and SOL scan findings (install-console)."""
import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HTML = "process-report.html"
JSON = "process-report.json"
_BAD = ("fail", "unmeasured", "interrupted")


def _ts(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _secs(a, b) -> int | None:
    a, b = _ts(a), _ts(b)
    return int((b - a).total_seconds()) if a and b else None


def dur(s: int | None) -> str:
    if s is None:
        return ""
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def _verdict(r) -> str:
    return r["verdict"] or r["status"]


def _headline(det) -> str:
    det = det or {}
    for k in ("headline", "error", "reason"):
        if det.get(k):
            return str(det[k])
    return ""


def _pass_of(passes, mark: str, row_id: int) -> int:
    """The pass a row belongs to: the last whose `mark` (the table's highest id when it
    started) is below the row's id."""
    out = passes[0]["id"]
    for p in passes[1:]:
        if row_id > p[mark]:
            out = p["id"]
    return out


def _link_kind(prev, prev_kind, state) -> str:
    """pull = a link that was up went down; re-plug = back up after a pull (a link first
    read down that comes up is just 'came up')."""
    if state == "down":
        return "pull" if prev in ("ok", "up", "wrong") else "change"
    if state in ("ok", "up") and prev == "down":
        return "re-plug" if prev_kind == "pull" else "came up"
    if state == "wrong":
        return "wrong port"
    return "change"


def _dev_order(g) -> dict[str, int]:
    keys = []
    for d in g.devices:
        keys.append(d.pos)
        keys += [f"{d.pos}.iom{n}" for n in range(1, d.endpoints.get("ioms", 0) + 1)]
    keys += [f"_sw-{s.id}" for s in g.switches] + ["_rack"]
    return {k: i for i, k in enumerate(keys)}


def build(order_dir, g, db) -> dict:
    """The report's data (JSON-able)."""
    passes = db.passes()
    runs = db.runs_all()
    waivers = db.waivers_all()
    events = db.events_all()
    links = db.link_events_all()
    order = _dev_order(g)
    out = []
    for i, p in enumerate(passes):
        nxt = passes[i + 1]["started_at"] if i + 1 < len(passes) else None
        out.append({"id": p["id"], "started_at": p["started_at"], "ended_at": nxt,
                    "who": p["who"], "reason": p["reason"], "devices": {}, "failures": {},
                    "verdicts": {}, "waivers": [], "interruptions": [], "restarts": [],
                    "links": [], "sol": [], "errors": [], "first": None, "last": None})
    by = {p["id"]: p for p in out}
    for r in runs:
        p = by[_pass_of(passes, "run_after", r["id"])]
        v = _verdict(r)
        secs = _secs(r["started_at"], r["finished_at"])
        arts = {k: a for k, a in (r["artifacts"] or {}).items()
                if isinstance(a, str) and a.startswith("artifacts/")}
        a = {"run": r["id"], "stage": r["stage"], "rung": r["rung"], "verdict": v,
             "started_at": r["started_at"], "finished_at": r["finished_at"], "seconds": secs,
             "headline": _headline(r["detail"]), "artifacts": arts}
        p["devices"].setdefault(r["pos"], []).append(a)
        p["verdicts"][v] = p["verdicts"].get(v, 0) + 1
        p["first"] = min(filter(None, (p["first"], r["started_at"])))
        p["last"] = max(filter(None, (p["last"], r["finished_at"] or r["started_at"])))
        if v in _BAD:
            cat = p["failures"].setdefault(f"{r['stage']} · {r['rung']} · {v}",
                                           {"count": 0, "devices": []})
            cat["count"] += 1
            if r["pos"] not in cat["devices"]:
                cat["devices"].append(r["pos"])
        if v == "interrupted":
            p["interruptions"].append({"pos": r["pos"], **a})
        det = r["detail"] or {}
        if det.get("findings"):
            p["sol"].append({"pos": r["pos"], "run": r["id"], "rung": r["rung"],
                             "findings": det["findings"], "counts": det.get("counts") or {},
                             "artifact": arts.get("sol")})
    for w in waivers:
        by[_pass_of(passes, "waiver_after", w["id"])]["waivers"].append(w)
    for e in events:
        p = by[_pass_of(passes, "event_after", e["id"])]
        if e["kind"] == "restart":
            p["restarts"].append(e)
        elif e["kind"] == "error":
            p["errors"].append(e)
    prev: dict[tuple, tuple] = {}
    for e in links:
        k = (e["pos"], e["link"])
        was, was_kind = prev.get(k, (None, None))
        kind = _link_kind(was, was_kind, e["state"])
        by[_pass_of(passes, "link_after", e["id"])]["links"].append(
            {**e, "was": was, "kind": kind})
        prev[k] = (e["state"], kind)
    totals: dict[str, int] = {}
    for p in out:
        p["seconds"] = _secs(p["first"], p["last"])
        p["attempts"] = sum(p["verdicts"].values())
        p["devices"] = {k: p["devices"][k]
                        for k in sorted(p["devices"], key=lambda k: (order.get(k, 1e9), k))}
        p["failures"] = [{"category": c, **x} for c, x in
                         sorted(p["failures"].items(), key=lambda kv: (-kv[1]["count"], kv[0]))]
        for f in p["failures"]:
            totals[f["category"]] = totals.get(f["category"], 0) + f["count"]
        p["pulls"] = sum(1 for e in p["links"] if e["kind"] == "pull")
        p["replugs"] = sum(1 for e in p["links"] if e["kind"] == "re-plug")
    return {"order": g.order, "customer": g.customer, "rack": g.rack, "prefix": g.url_prefix,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "closed_at": db.closed_at(), "passes": out,
            "failure_totals": [{"category": c, "count": n} for c, n in
                               sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))]}


def _atomic(path: Path, text: str) -> None:
    """Write via a temp file of this writer's own (two concurrent writers never share
    one), then rename over path."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write(order_dir, g, db) -> Path:
    """process-report.json + process-report.html into order_dir (atomic). -> the html."""
    order_dir = Path(order_dir)
    data = build(order_dir, g, db)
    _atomic(order_dir / JSON, json.dumps(data, indent=1) + "\n")
    _atomic(order_dir / HTML, render(data))
    return order_dir / HTML


# ---------------------------------------------------------------- html
_CSS = """
:root{--bg:#0e1117;--panel:#161b22;--panel2:#1c2230;--border:#2b3340;--text:#c9d1d9;
--muted:#8b949e;--dim:#5b636e;--accent:#58a6ff;--green:#3fb950;--amber:#d29922;--red:#f85149;
--grey:#6e7681;--violet:#a371f7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.mono,td.m{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
header{position:sticky;top:0;background:var(--panel);border-bottom:1px solid var(--border);
padding:.55rem 1rem;display:flex;flex-wrap:wrap;gap:.4rem 1.2rem;align-items:baseline;z-index:2}
header b{font-size:1rem;letter-spacing:.4px} header span{color:var(--muted)}
main{padding:.6rem 1rem 3rem;max-width:1400px}
h2{font-size:.95rem;margin:1.6rem 0 .3rem;padding:.3rem .5rem;background:var(--panel2);
border:1px solid var(--border);border-radius:6px}
h3{font-size:.72rem;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin:1rem 0 .25rem}
h4{font-size:.8rem;margin:.8rem 0 .2rem;font-family:ui-monospace,monospace}
.sub{color:var(--muted);font-size:.78rem;margin:.1rem 0 .4rem}
table{border-collapse:collapse;width:100%;margin:.15rem 0 .4rem;font-size:.78rem}
th{text-align:left;font-weight:600;color:var(--muted);font-size:.66rem;text-transform:uppercase;
letter-spacing:.4px;border-bottom:1px solid var(--border);padding:.2rem .45rem}
td{border-bottom:1px solid #1d232d;padding:.18rem .45rem;vertical-align:top}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.hl{color:var(--muted);max-width:640px;overflow-wrap:anywhere}
td.t{white-space:nowrap;font-variant-numeric:tabular-nums}
.v{font-family:ui-monospace,monospace;font-size:.72rem;padding:0 .3rem;border-radius:3px;border:1px solid var(--border)}
.v.pass{color:var(--green)} .v.fail{color:var(--red);border-color:rgba(248,81,73,.45)}
.v.unmeasured,.v.skip{color:var(--grey)} .v.interrupted,.v.running{color:var(--amber)}
.k.pull{color:var(--red)} .k.re-plug{color:var(--green)} .k.wrong{color:var(--amber)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
.none{color:var(--dim);font-size:.75rem;margin:.1rem 0 .3rem}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin:.2rem 0}
.chip{border:1px solid var(--border);border-radius:6px;padding:.1rem .45rem;background:var(--panel2);font-size:.75rem}
.chip b{font-size:.85rem;margin-right:.25rem}
.chip.bad b{color:var(--red)} .chip.warn b{color:var(--amber)}
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _table(head, rows, cls=None) -> str:
    if not rows:
        return '<p class="none">none</p>'
    h = "".join(f"<th>{_e(x)}</th>" for x in head)
    body = []
    for r in rows:
        body.append("<tr>" + "".join(
            f'<td class="{(cls or {}).get(i, "")}">{c}</td>' for i, c in enumerate(r)) + "</tr>")
    return f"<table><tr>{h}</tr>{''.join(body)}</table>"


def _t(iso) -> str:
    """A table timestamp: 'MM-DD HH:MM:SS' (UTC; the full stamp in the tooltip)."""
    if not iso:
        return ""
    return f'<span title="{_e(iso)}">{_e(iso[5:10])} {_e(iso[11:19])}</span>'


_LINK_SHOWN = ("pull", "re-plug", "wrong port")


def _v(v) -> str:
    return f'<span class="v {_e(v)}">{_e(v)}</span>'


def _arts(a: dict) -> str:
    return " ".join(f'<a href="{_e(p)}">{_e(k)}</a>' for k, p in sorted(a.items()))


def _chip(n, label, bad=False) -> str:
    c = " bad" if bad and n else ""
    return f'<span class="chip{c}"><b>{n}</b>{_e(label)}</span>'


def render(data: dict) -> str:
    t = [f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
         f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
         f"<title>{_e(data['prefix'])} process report</title><style>{_CSS}</style></head><body>",
         f"<header><b>Process report · {_e(data['order'])}"
         f"{' rack ' + _e(data['rack']) if data['rack'] else ''}</b>"
         f"<span>{_e(data['customer'])}</span><span>internal: not for the customer</span>"
         f"<span>generated {_e(data['generated_at'])}</span>"
         + (f"<span>closed {_e(data['closed_at'])}</span>" if data["closed_at"] else "")
         + "</header><main>"]
    ps = data["passes"]
    t.append("<h3>passes</h3>")
    t.append(_table(
        ("pass", "started", "by", "reason", "attempts", "fail", "not measured", "interrupted",
         "waivers", "pulls", "SOL findings", "wall time"),
        [[f'<a href="#pass{p["id"]}">{p["id"]}</a>', _e(p["started_at"]), _e(p["who"]),
          _e(p["reason"]), p["attempts"], p["verdicts"].get("fail", 0),
          p["verdicts"].get("unmeasured", 0), p["verdicts"].get("interrupted", 0),
          len(p["waivers"]), p["pulls"], sum(s["findings"] for s in p["sol"]),
          dur(p["seconds"])] for p in ps],
        {4: "n", 5: "n", 6: "n", 7: "n", 8: "n", 9: "n", 10: "n", 11: "n"}))
    t.append("<h3>failure categories, all passes</h3>")
    t.append(_table(("stage · rung · verdict", "count"),
                    [[_e(f["category"]), f["count"]] for f in data["failure_totals"]],
                    {1: "n"}))
    for p in ps:
        t.append(f'<h2 id="pass{p["id"]}">Pass {p["id"]}</h2>')
        t.append(f'<p class="sub">started {_e(p["started_at"])} by {_e(p["who"])}: '
                 f'{_e(p["reason"])}' + (f' · ended {_e(p["ended_at"])} (start fresh)'
                                         if p["ended_at"] else "")
                 + (f' · attempts ran {_e(p["first"])} → {_e(p["last"])} ({dur(p["seconds"])})'
                    if p["first"] else "") + "</p>")
        t.append('<div class="chips">' + "".join(
            _chip(p["verdicts"].get(k, 0), lbl, k in _BAD)
            for k, lbl in (("pass", "pass"), ("fail", "fail"), ("unmeasured", "not measured"),
                           ("interrupted", "interrupted"), ("skip", "skip")))
            + _chip(len(p["waivers"]), "waivers") + _chip(p["pulls"], "cable pulls", True)
            + _chip(p["replugs"], "re-plugs")
            + _chip(sum(s["findings"] for s in p["sol"]), "SOL findings", True) + "</div>")
        t.append("<h3>failure categories</h3>")
        t.append(_table(("stage · rung · verdict", "count", "devices"),
                        [[_e(f["category"]), f["count"], _e(" ".join(f["devices"]))]
                         for f in p["failures"]], {1: "n", 2: "m"}))
        t.append("<h3>waivers</h3>")
        t.append(_table(("at", "device", "stage", "rungs", "who", "why"),
                        [[_t(w["at"]), _e(w["pos"]), _e(w["stage"]), _e(", ".join(w["rungs"])),
                          _e(w["who"]), _e(w["reason"])] for w in p["waivers"]],
                        {0: "t", 5: "hl"}))
        t.append("<h3>interruptions (daemon restarts)</h3>")
        rows = [[_t(e["ts"]), "restart", "", _e(e["text"])] for e in p["restarts"]]
        rows += [[_t(i["started_at"]), _e(i["pos"]), _e(f"{i['stage']} · {i['rung']}"),
                  f"cut short after {dur(i['seconds'])}"] for i in p["interruptions"]]
        t.append(_table(("at", "device", "rung", "what"), sorted(rows), {0: "t", 3: "hl"}))
        t.append("<h3>link events (cable pulls / re-plugs)</h3>")
        shown = [e for e in p["links"] if e["kind"] in _LINK_SHOWN]
        t.append(_table(("at", "device", "link", "was → now", "kind", "seen"),
                        [[_t(e["at"]), _e(e["pos"]), _e(e["link"]),
                          _e(f"{e['was'] or '–'} → {e['state']}"),
                          f'<span class="k {_e(e["kind"].split()[0])}">{_e(e["kind"])}</span>',
                          _e(e["text"])] for e in shown], {0: "t", 5: "hl"}))
        if len(shown) < len(p["links"]):
            t.append(f'<p class="none">{len(p["links"]) - len(shown)} other transition(s) '
                     "(first reads, came up, identity seen, not answering) in "
                     f"{JSON}</p>")
        t.append("<h3>SOL scan findings</h3>")
        t.append(_table(("device", "run", "rung", "findings", "patterns", "console"),
                        [[_e(s["pos"]), s["run"], _e(s["rung"]), s["findings"],
                          _e(", ".join(f"{k} ×{n}" for k, n in s["counts"].items())),
                          f'<a href="{_e(s["artifact"])}">sol</a>' if s["artifact"] else ""]
                         for s in p["sol"]], {1: "n", 3: "n"}))
        if p["errors"]:
            t.append("<h3>errors</h3>")
            t.append(_table(("at", "what"), [[_t(e["ts"]), _e(e["text"])] for e in p["errors"]],
                            {0: "t", 1: "hl"}))
        t.append("<h3>attempts per device</h3>")
        if not p["devices"]:
            t.append('<p class="none">no attempts in this pass</p>')
        for pos, atts in p["devices"].items():
            bad = sum(1 for a in atts if a["verdict"] in _BAD)
            t.append(f'<h4 id="p{p["id"]}-{_e(pos)}">{_e(pos)} <span class="sub">'
                     f'{len(atts)} attempt(s){f", {bad} not passed" if bad else ""}</span></h4>')
            t.append(_table(("run", "stage", "rung", "verdict", "started", "took", "headline",
                             "artifacts"),
                            [[a["run"], _e(a["stage"]), _e(a["rung"]), _v(a["verdict"]),
                              _t(a["started_at"]), dur(a["seconds"]), _e(a["headline"]),
                              _arts(a["artifacts"])] for a in atts],
                            {0: "n", 4: "t", 5: "n", 6: "hl"}))
    t.append("</main></body></html>\n")
    return "".join(t)
