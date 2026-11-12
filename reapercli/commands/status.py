"""reaper status [site|all] [--full] [--json]: one screen per site (spec §7).

Default: every HA row, then per section either `N ok` or the components that are
not ok (rolled up per bang). --full prints every row; `report` writes them all.
"""
import argparse

from reapercli.lib.gather import LANE_ORDER, gather, site_rows
from reapercli.lib.health import rollup
from reapercli.lib.out import Row, exit_code, render
from reapercli.lib.sites import resolve_scope


def summarize(rows: list) -> list:
    out = [r for r in rows if r.section == "ha"]
    for section in LANE_ORDER[1:]:
        rolled = rollup([r for r in rows if r.section == section])
        if not rolled:
            continue
        bad = [r for r in rolled if r.status not in ("ok", "dormant")]
        if section == "unmanaged":
            bad = []
            out.append(Row(section, "all", "info", f"{len(rolled)} unmanaged item(s); see report",
                           site=rolled[0].site))
        elif not bad:
            out.append(Row(section, "all", "ok", f"{len(rolled)} ok", site=rolled[0].site))
        out += bad
    return out


def run(argv: list, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog="reaper status")
    p.add_argument("--full", action="store_true", help="every row, nothing rolled up")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(rest)
    all_rows, shown = [], []
    for view in gather(sites, ctx):
        rows = site_rows(view)
        all_rows += rows
        shown += rows if args.full or args.json else summarize(rows)
    print(render(shown, as_json=args.json))
    return exit_code(all_rows)
