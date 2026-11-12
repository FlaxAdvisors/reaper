"""reaper diag's bundle: summary.txt (header with each source's reach, NOW, TIMELINE),
raw/<bang>/<source>.txt, and a tarball beside the directory. Facts only."""
import json
import os
import tarfile
from datetime import timezone
from pathlib import Path

from reapercli.lib.timeline import UTC, events, fold, journal, reach
from reapercli.probe_remote import parse_df, parse_ss_listen

DISK_PCT = 85


def failed(s: dict) -> bool:
    """A source that gave nothing it could have: timeout, or an error with no output.
    systemctl's 3 (inactive) and 4 (no such unit) are information, not failure; from
    any other command they are a failure like any rc."""
    ok = (0, 3, 4) if str(s.get("cmd", "")).startswith("systemctl") else (0,)
    return not s.get("absent") and (s.get("rc") == 124 or (s.get("rc") not in ok and not s.get("out")))


def _pg(f: dict) -> str:
    if f.get("in_recovery") == "t":
        return (f"postgres standby, wal_receiver {f.get('wal_receiver') or 'NONE'}, replay lag "
                f"{f.get('replay_lag_bytes')} B, standby.signal {'yes' if f.get('standby_signal') else 'NO'}")
    reps = [r.split("|") for r in (f.get("replication") or "").splitlines() if r.count("|") == 2]
    rep = ", ".join(f"{a} {st} lag {lag} B" for a, st, lag in reps) or "NO standby streaming"
    return f"postgres primary, replication {rep}; alembic {f.get('alembic')}"


def now_block(collected: dict, sh, ports: dict) -> list:
    from reapercli.lib.extras import parse_extras
    out = ["NOW (as collected, not windowed)"]
    holders = [b for b, d in collected.items()
               if f"inet {sh.vip}/" in d.get("sources", {}).get("host.ipaddr", {}).get("out", "")]
    out.append(f"  VIP holder: {holders[0] if len(holders) == 1 else (', '.join(holders) or 'NONE')}")
    for bang, d in collected.items():
        if "sources" not in d:
            out.append(f"  {bang}: {'dormant' if d.get('dormant') else 'NOT COLLECTED: ' + d.get('error', '?')}")
            continue
        s = {k: v.get("out", "") for k, v in d["sources"].items()}
        try:
            pg = _pg(json.loads(s.get("pg.facts") or "{}"))
        except ValueError:
            pg = "postgres facts unreadable"
        out.append(f"  {bang}: keepalived {s.get('keepalived.active', '').strip() or '?'}; {pg}")
        bad = [l.split()[0] for l in s.get("host.failed", "").splitlines() if l.strip()]
        if bad:
            out.append(f"  {bang}: failed units: {' '.join(bad)}")
        for line in s.get("docker.ps", "").splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            if c.get("State") != "running":
                out.append(f"  {bang}: container {c.get('Names')} {c.get('State')} ({c.get('Status')})")
        for fs in parse_df(s.get("host.df", "")):
            if fs["used_pct"] >= DISK_PCT:
                out.append(f"  {bang}: disk {fs['mount']} {fs['used_pct']}%")
        missing = sorted(set(ports.get(bang, ())) - set(parse_ss_listen(s.get("host.ss", ""))))
        if missing:
            out.append(f"  {bang}: ports not listening: {' '.join(map(str, missing))}")
        if s.get("site.extras"):
            out += [f"  {r.section} {r.item}: {r.status} {r.value}" for r in parse_extras(s["site.extras"], sh.site)]
    return out


def _render_raw(name: str, s: dict) -> str:
    head = f"# rc={s.get('rc')} {s.get('err', '').strip()}\n" if failed(s) else ""
    if s.get("absent"):
        return "# absent on this bang\n"
    if name.endswith(".journal"):
        body = "".join(f"{t.strftime('%Y-%m-%dT%H:%M:%S.%fZ')} {ident}: {msg}\n" for t, _p, ident, msg in journal(s.get("out", "")))
    else:
        body = s.get("out", "")
    return head + body + ("\n# truncated: newest 5 MB kept\n" if s.get("truncated") else "")


def _t(dt, tz, fmt="%Y-%m-%d %H:%M"):
    return dt.astimezone(tz).strftime(fmt) if dt else "?"


def header(site: str, names: list, win, collected: dict, tz, collected_at) -> list:
    zone = collected_at.astimezone(tz).strftime("%Z")
    out = [f"reaper diag {site}  services: {' '.join(names) if names else 'all'}",
           f"window: {_t(win.start, tz)} -> {_t(win.end, tz)} {zone}{'  (clamped to 72 h)' if win.clamped else ''}",
           f"collected: {_t(collected_at, tz, '%Y-%m-%d %H:%M:%S')} {zone}"]
    for bang, d in collected.items():
        if "error" in d:
            out.append(f"failed: {bang}: {d['error']}")
        for n, s in sorted(d.get("sources", {}).items()):
            if failed(s):
                out.append(f"failed: {bang} {n}: rc={s.get('rc')} {s.get('err', '').strip()[:120]}")
    for bang, d in collected.items():
        for label, since, cut in reach(bang, d.get("sources", {}), win):
            what = f"since {_t(since, tz)}" if since else "start unknown"
            out.append(f"reach: {bang} {label}: {what}{', truncated to newest 5 MB' if cut else ''}")
    return out


def timeline(collected: dict, win, tz) -> list:
    evs = fold([e for bang, d in collected.items() for e in events(bang, d.get("sources", {}), win)])
    evs.sort(key=lambda e: (e.t, e.bang, e.source))
    out, day = [f"TIMELINE ({win.end.astimezone(tz).strftime('%Z')})"], None
    for e in evs:
        local = e.t.astimezone(tz)
        if local.date() != day:
            day = local.date()
            out.append(f"--- {day.isoformat()} ---")
        more = f" (x{e.count} until {e.last.astimezone(tz).strftime('%H:%M:%S')})" if e.count > 1 else ""
        out.append(f"{local.strftime('%H:%M:%S')}  {e.bang}  {e.source}  {e.text}{more}")
    if len(out) == 1:
        out.append("(no events in the window)")
    return out


def write_bundle(out: Path, site: str, names: list, win, collected: dict, sh, tz, ports: dict,
                 collected_at) -> Path:
    """Private (0700 dir, 0600 files): the bundle holds cleartext secrets."""
    old = os.umask(0o077)
    try:
        return _write(out, site, names, win, collected, sh, tz, ports, collected_at)
    finally:
        os.umask(old)


def _write(out, site, names, win, collected, sh, tz, ports, collected_at) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    for bang, d in collected.items():
        raw = out / "raw" / bang
        raw.mkdir(parents=True, exist_ok=True)
        if "sources" not in d:
            (raw / "NOT-COLLECTED.txt").write_text(json.dumps(d) + "\n")
        for name, s in d.get("sources", {}).items():
            (raw / f"{name}.txt").write_text(_render_raw(name, s))
    now = now_block(collected, sh, ports)
    text = header(site, names, win, collected, tz, collected_at) + [""] + now + [""] + timeline(collected, win, tz)
    (out / "summary.txt").write_text("\n".join(text) + "\n")
    tar = out.parent / f"{out.name}.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        t.add(out, arcname=out.name)
    tar.chmod(0o600)
    return tar
