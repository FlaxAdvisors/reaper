"""reaper diag [site|all] [service...] [--since WHEN] [--window LENGTH]: collect read-only
from every live bang, line it up, write a bundle (spec 2026-11-08-reaper-diag-design).
Interpreting the bundle is a conversation, not this command. The bundle holds cleartext
secrets (compose env files, docker inspect env): it stays on this machine."""
import argparse
import sys
from datetime import datetime, timezone

from reapercli.commands.report import report_dir
from reapercli.lib.components import expected_running, load_components, placed_on
from reapercli.lib.diagout import failed, now_block, write_bundle
from reapercli.lib.diagscope import collect, remote_spec, resolve
from reapercli.lib.diagwin import window
from reapercli.lib.guards import Refused
from reapercli.lib.inventory import site_hosts
from reapercli.lib.sites import ScopeError, load_sites, resolve_scope


class _Args(argparse.ArgumentParser):
    def error(self, message):
        raise ScopeError(f"diag: {message}")


def _ports(names, comps, sh, site, root, holder) -> dict:
    out = {}
    for b in sh.bangs:
        for n in names:
            c = comps[n]
            if c.ports and expected_running(c, b.name, placed_on(c, sh, site, root), holder):
                out.setdefault(b.name, set()).update(c.ports)
    return out


def run(argv: list, ctx, tz=None, now=None) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    p = _Args(prog="reaper diag")
    p.add_argument("services", nargs="*")
    p.add_argument("--since")
    p.add_argument("--window")
    a = p.parse_args(rest)
    now = now or datetime.now().astimezone()
    # no injected zone: the system's own, DST-aware per date (tz None = astimezone())
    win = window(a.since, a.window, now.astimezone(tz) if tz else now, local=tz is None)
    comps = load_components(ctx.root, load_sites(ctx.root))
    names = resolve(a.services, comps)
    rc = 0
    for site in sites:
        stamp = now.astimezone(timezone.utc)
        out = report_dir(ctx, site.name, stamp).parent / f"diag-{stamp.strftime('%Y%m%dT%H%M%SZ')}"
        if out.resolve().is_relative_to(ctx.root.resolve()):
            raise Refused(f"diag bundle {out} would be inside {ctx.root}: it holds cleartext secrets")
        sh = site_hosts(site, ctx.root, ctx.hostname)
        collected = collect(sh, remote_spec(names, comps, sh, win), runner=ctx.runner)
        holders = [b for b, d in collected.items()
                   if f"inet {sh.vip}/" in d.get("sources", {}).get("host.ipaddr", {}).get("out", "")]
        ports = _ports(names, comps, sh, site, ctx.root, holders[0] if len(holders) == 1 else None)
        tar = write_bundle(out, site.name, a.services, win, collected, sh, tz, ports, datetime.now(timezone.utc))
        print(f"{site.name}: {out / 'summary.txt'}\n{site.name}: {tar}")
        print("\n".join(now_block(collected, sh, ports)))
        if any("error" in d or any(failed(s) for s in d.get("sources", {}).values()) for d in collected.values()):
            print(f"{site.name}: some sources were not collected: see the summary header", file=sys.stderr)
            rc = 1
    return rc
