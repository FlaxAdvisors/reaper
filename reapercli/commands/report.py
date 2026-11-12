"""reaper report [site|all] [--html]: the health and status of everything reaper
manages, written to a file per site (spec §7.1, §9). Exits 1 on any FAIL, so it
can run from cron."""
import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from reapercli.lib.extras import SCRIPT, parse_extras
from reapercli.lib.gather import gather, site_rows
from reapercli.lib.inventory import ssh_target
from reapercli.lib.out import STATUSES, Row, exit_code, render
from reapercli.lib.remote import fanout
from reapercli.lib.report_html import render_html
from reapercli.lib.sites import resolve_scope

ORDER = {"FAIL": 0, "unreachable": 1, "warn": 2}


def report_dir(ctx, site: str, now: datetime) -> Path:
    devel = ctx.home / "git" / "reaper-devel"
    base = devel / "reports" if devel.is_dir() else ctx.home / "reaper-reports"
    return base / site / now.strftime("%Y%m%dT%H%M%SZ")


def summary_line(rows: list) -> str:
    n = {s: sum(r.status == s for r in rows) for s in STATUSES}
    return "SUMMARY " + ", ".join(f"{v} {k}" for k, v in n.items())


def write_report(rows: list, out: Path, html: bool) -> list:
    out.mkdir(parents=True, exist_ok=True)
    bad = sorted((r for r in rows if r.status in ORDER), key=lambda r: ORDER[r.status])
    text = [summary_line(rows), "", render(bad) if bad else "nothing FAIL, unreachable or warn", "", "ALL ROWS", render(rows)]
    paths = [out / "report.txt", out / "report.json"]
    paths[0].write_text("\n".join(text) + "\n")
    paths[1].write_text(json.dumps([asdict(r) for r in rows], indent=1))
    if html:
        paths.append(out / "report.html")
        paths[2].write_text(render_html(f"reaper report {out.parent.name} {out.name}", summary_line(rows),
                                        bad + rows))
    return paths


def extras(view, ctx) -> list:
    holder = next((b for b in view.hosts.bangs if b.name == view.vip_holder), None)
    if holder is None:
        return [Row("alerts", "extras", "warn", "no VIP holder to ask", site=view.site.name)]
    r = fanout({holder.name: ssh_target(holder, view.hosts.on_site)}, "bash -s", SCRIPT, 30, ctx.runner)[holder.name]
    return parse_extras(r.out, view.site.name)


def run(argv: list, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog="reaper report")
    p.add_argument("--html", action="store_true")
    args = p.parse_args(rest)
    now, all_rows = datetime.now(timezone.utc), []
    for view in gather(sites, ctx):
        rows = site_rows(view) + extras(view, ctx)
        all_rows += rows
        for path in write_report(rows, report_dir(ctx, view.site.name, now), args.html):
            print(path)
        print(f"{view.site.name}: {summary_line(rows)}")
    return exit_code(all_rows)
