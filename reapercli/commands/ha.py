"""reaper ha status|check|quiesce|arm|keepalived-roll|reseed [site|all] (spec §6, §8.3)."""
import argparse

from reapercli.lib.gather import gather
from reapercli.lib.ha import ha_rows
from reapercli.lib.hapath import ha_check
from reapercli.lib.inventory import site_hosts
from reapercli.lib.out import exit_code, render
from reapercli.lib.sites import resolve_scope

USAGE = "usage: reaper ha status | check | quiesce | arm | keepalived-roll | reseed [site|all] [--yes] [--confirm T]"
MUTATORS = ("quiesce", "arm", "keepalived-roll", "reseed")


def run(argv: list, ctx) -> int:
    if not argv or argv[0] not in ("status", "check", *MUTATORS):
        print(USAGE)
        return 2
    action = argv[0]
    sites, rest = resolve_scope(argv[1:], ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog=f"reaper ha {action}")
    if action in MUTATORS:
        p.add_argument("--yes", action="store_true")
    if action == "reseed":
        p.add_argument("--confirm")
    a = p.parse_args(rest)
    if action == "status":
        rows = []
        for v in gather(sites, ctx):
            rows += [r.__class__(**{**r.__dict__, "site": v.site.name}) for r in ha_rows(v.hosts, v.facts)]
        print(render(rows))
        return exit_code(rows)
    rc = 0
    for site in sites:
        if action == "check":
            rc |= ha_check(site, ctx)
            continue
        from reapercli.lib import hactl
        sh = site_hosts(site, ctx.root, ctx.hostname)
        hactl.refuse_dormant(site, sh)
        if action == "reseed":
            rc |= hactl.reseed(site, sh, ctx, a.confirm, a.yes)
            continue
        fn = {"quiesce": hactl.quiesce, "arm": hactl.arm, "keepalived-roll": hactl.roll}[action]
        rc |= fn(site, sh, ctx, a.yes)
    return 1 if rc else 0
