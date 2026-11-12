"""Per-bang health rows from probe facts + the registry (spec §7, §7.1).

Each row's item is `<component>/<thing>`, so rollup() can fold a component into
one line for `status` while `report` keeps every line.
"""
import time

from reapercli.lib.components import expected_running
from reapercli.lib.out import UNREACHABLE, Row, worst

DISK_WARN, DISK_FAIL = 85, 95
KEY_MOUNTS = ("/", "/opt/flax/var/postgres", "/export", "/srv")


def host_rows(bang: str, facts: dict, peers: dict) -> list:
    """peers: the site's other bangs -> dormant?; only their tailscale paths are shown."""
    if facts.get("dormant"):
        return [Row("host", "reach", "dormant", "dormant", bang)]
    if "error" in facts:
        return [Row("host", "reach", UNREACHABLE if facts.get("unreachable") else "FAIL", facts["error"], bang)]
    h, rows = facts.get("host") or {}, []
    if "error" in h:
        return [Row("host", "host", "FAIL", h["error"], bang)]
    for d in h.get("disks") if isinstance(h.get("disks"), list) else []:
        pct = d["used_pct"]
        status = "FAIL" if pct >= DISK_FAIL else "warn" if pct >= DISK_WARN else "ok"
        if d["mount"] in KEY_MOUNTS or status != "ok":
            rows.append(Row("host", f"disk {d['mount']}", status, f"{pct}%", bang))
    mem = h.get("mem_avail_pct")
    rows.append(Row("host", "memory", "warn" if mem is not None and mem < 10 else "ok", f"{mem}% available", bang))
    rows.append(Row("host", "load", "info", f"{h.get('load1')}", bang))
    failed = h.get("failed_units")
    if isinstance(failed, list):
        rows.append(Row("host", "failed units", "warn" if failed else "ok", ", ".join(failed) or "none", bang))
    off = h.get("chrony_offset_s")
    if isinstance(off, (int, float)):
        rows.append(Row("host", "time sync", "warn" if off > 0.5 else "ok", f"offset {off:g}s", bang))
    ts = h.get("tailscale") if isinstance(h.get("tailscale"), dict) else {}
    for peer, dormant in peers.items():
        path = ts.get(peer, "unknown")
        status = "info" if path == "relay" or dormant else "warn" if path in ("offline", "unknown") else "ok"
        rows.append(Row("host", f"tailscale -> {peer}", status, path, bang))
    return rows


def _row(c, kind, name, status, value, bang):
    return Row(c.lane, f"{c.name}/{name}", status, value if kind == "unit" else f"{kind}: {value}", bang)


def component_rows(bang, facts, comps, vip_holder, pins, placed) -> list:
    if "error" in facts or facts.get("dormant"):
        return []
    rows = []
    for c in comps.values():
        on = bang in placed.get(c.name, ())
        exp = expected_running(c, bang, placed.get(c.name, ()), vip_holder)
        miss = "info" if c.optional else "FAIL"
        units = facts.get("units") or {}
        for u in c.units:
            if u in c.timers:
                continue                       # a timer-driven oneshot: the timer row covers it
            if "error" in units:
                rows.append(_row(c, "unit", u, "FAIL", f"units: {units['error']}", bang)); continue
            d = units.get(u, {})
            present, active = d.get("load", "not-found") != "not-found", d.get("active") == "active"
            if exp:
                rows.append(_row(c, "unit", u, "ok" if active else (miss if not present else "FAIL"),
                                 f"{d.get('active', 'absent')} {d.get('sub', '')}".strip(), bang))
            elif active:
                rows.append(_row(c, "unit", u, "warn", "running here, not expected", bang))
            elif on and present:
                rows.append(_row(c, "unit", u, "ok", "standby", bang))
        cons = facts.get("containers") or {}
        for n in c.containers:
            if "error" in cons:
                rows.append(_row(c, "container", n, "FAIL", cons["error"], bang)); continue
            d = cons.get(n)
            if exp:
                rows.append(_container(c, n, d, pins, bang, miss))
            elif d and d.get("state") == "running":
                rows.append(_row(c, "container", n, "warn", "running here, not expected", bang))
            elif d and on:
                rows.append(Row(c.lane, f"{c.name}/{n}", "ok", f"standby ({d.get('state')})", bang))
        if not on:
            continue
        timers = facts.get("timers") or {}
        for t in c.timers:
            if "error" in timers:
                rows.append(_row(c, "timer", t, "FAIL", timers["error"], bang)); continue
            d = timers.get(t)
            age = f"last {int(time.time() - d['last'])}s ago" if d and d.get("last") else "never ran"
            rows.append(_row(c, "timer", t, "ok" if d else miss, age if d else "absent", bang))
        crons = facts.get("crons") or []
        for n in c.crons:
            if isinstance(crons, dict):
                rows.append(_row(c, "cron", n, "FAIL", crons.get("error", "?"), bang)); continue
            rows.append(_row(c, "cron", n, "ok" if n in crons else miss, "present" if n in crons else "absent", bang))
        files = facts.get("files") or {}
        for p in c.files:
            d = files.get(p, {})
            st = "ok" if d.get("exists") and not d.get("error") else ("warn" if d.get("exists") else miss)
            rows.append(_row(c, "file", p, st, d.get("stamp") or d.get("error") or ("present" if d.get("exists") else "absent"), bang))
        if exp:
            listen = facts.get("listen") or []
            for port in c.ports:
                if isinstance(listen, dict):
                    rows.append(_row(c, "port", f"port {port}", "FAIL", listen.get("error", "?"), bang)); continue
                rows.append(_row(c, "port", f"port {port}", "ok" if port in listen else "FAIL",
                                 "listening" if port in listen else "not listening", bang))
        if c.freshness and bang == vip_holder:
            pg = facts.get("pg") or {}
            age = (pg.get("freshness") or {}).get(c.freshness["table"])
            if "error" in pg:
                rows.append(_row(c, "freshness", "freshness", "FAIL", pg["error"], bang))
            elif isinstance(age, dict):
                rows.append(_row(c, "freshness", "freshness", "FAIL", age.get("error", "?"), bang))
            elif not isinstance(age, int) or age < 0:
                rows.append(_row(c, "freshness", "freshness", "FAIL", f"no reading ({age})", bang))
            else:
                st = "ok" if age <= c.freshness["warn_s"] else "warn" if age <= c.freshness["fail_s"] else "FAIL"
                rows.append(_row(c, "freshness", "freshness", st, f"{c.freshness['table']} {age}s old", bang))
    return rows


def _container(c, n, d, pins, bang, miss):
    if not d:
        return _row(c, "container", n, miss, "absent", bang)
    if d.get("state") != "running":
        return _row(c, "container", n, "FAIL", d.get("state", "?"), bang)
    if d.get("health") == "unhealthy":
        return _row(c, "container", n, "FAIL", "unhealthy", bang)
    repo, _, tag = d.get("image", "").rpartition(":")
    pin = pins.get(c.image_var or "") if repo == c.image_repo else None   # e.g. nginx:1.27 is not pinned
    if pin and tag != pin:
        return _row(c, "container", n, "warn", f"image {tag} != pin {pin}", bang)
    return _row(c, "container", n, "ok", f"running {d.get('health') or ''} {tag}".replace("  ", " ").strip(), bang)


def unmanaged_rows(bang, facts, comps) -> list:
    if "error" in facts or facts.get("dormant"):
        return []
    claimed = {x for c in comps.values() for x in (*c.units, *c.timers, *c.containers)}
    rows = []
    cons = facts.get("containers") or {}
    for n in sorted(k for k in cons if k not in claimed and k != "error"):
        rows.append(Row("unmanaged", n, "info", f"unmanaged container ({cons[n].get('state')})", bang))
    for u in (facts.get("units") or {}).get("_present", []):
        stem = u.rsplit(".", 1)[0]
        if stem not in claimed:
            rows.append(Row("unmanaged", u, "info", "unmanaged unit", bang))
    return rows


LANES = ("infra", "core", "triage", "post-loose", "post-rack")


def rollup(rows: list) -> list:
    groups = {}
    for r in rows:   # component lanes fold to the component; host/ha/unmanaged items stay whole
        comp = r.item.split("/", 1)[0] if r.section in LANES else r.item
        key = (r.site, r.host, r.section, comp)
        groups.setdefault(key, []).append(r)
    out = []
    for (site, host, section, comp), rs in groups.items():
        status = worst(r.status for r in rs)
        bad = [f"{r.item.split('/', 1)[-1] if section in LANES else ''}: {r.value}".lstrip(": ")
               for r in rs if r.status not in ("ok", "dormant")]
        value = "; ".join(bad) if bad else (rs[0].value if len(rs) == 1 else f"{len(rs)} ok")
        out.append(Row(section, comp, status, value, host, site))
    return out
