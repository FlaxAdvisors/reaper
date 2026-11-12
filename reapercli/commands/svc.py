"""reaper svc ls [site|all] [component...] | svc logs [site] <component|unit|container> (spec §6)."""
import argparse
import shlex

from reapercli.lib.components import load_components, placed_on
from reapercli.lib.gather import gather, site_rows
from reapercli.lib.hapath import vip_holder
from reapercli.lib.inventory import site_hosts, ssh_target
from reapercli.lib.out import exit_code, render
from reapercli.lib.remote import fanout
from reapercli.lib.sites import load_sites, resolve_scope


def resolve(name: str, comps: dict):
    """component -> its first unit (else container); a claimed unit or container as-is.
    A unit beats a same-named container: journald also holds a docker-run unit's output."""
    if name in comps:
        c = comps[name]
        return ("unit", c.units[0], c) if c.units else ("container", c.containers[0], c) if c.containers else None
    for c in comps.values():
        if name in c.units:
            return "unit", name, c
    for c in comps.values():
        if name in c.containers:
            return "container", name, c
    return None


def logs(argv, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog="reaper svc logs")
    p.add_argument("name")
    p.add_argument("-n", type=int, default=200)
    p.add_argument("--since", default="")
    p.add_argument("--host")
    a = p.parse_args(rest)
    comps = load_components(ctx.root, load_sites(ctx.root))
    found = resolve(a.name, comps)
    if not found or len(sites) != 1:
        print(f"reaper svc logs: {'name one site' if found else f'{a.name!r} is not a registered component, unit or container'}")
        return 2
    kind, name, c = found
    site = sites[0]
    sh = site_hosts(site, ctx.root, ctx.hostname)
    bang = a.host or (vip_holder(sh, ctx.runner) if c.vip_gated else None) or placed_on(c, sh, site, ctx.root)[0]
    since = f" --since {shlex.quote(a.since)}" if a.since else ""
    cmd = (f"sudo -n journalctl -u {shlex.quote(name)} -n {a.n}{since} --no-pager -o short-iso" if kind == "unit"
           else f"docker logs --tail {a.n}{since} {shlex.quote(name)} 2>&1")
    target = next(b for b in sh.bangs if b.name == bang)
    r = fanout({bang: ssh_target(target, sh.on_site)}, cmd, None, 60, ctx.runner)[bang]
    print(r.out, end="")
    if r.rc:
        print(r.err.strip())
    return 0 if r.rc == 0 else 1


def ls(argv, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    wanted = set(rest)
    rows = [r for v in gather(sites, ctx) for r in site_rows(v)
            if r.section not in ("ha", "host", "unmanaged") and (not wanted or r.item.split("/")[0] in wanted)]
    print(render(rows))
    return exit_code(rows)


def run(argv: list, ctx) -> int:
    if not argv or argv[0] not in ("ls", "logs", "restart"):
        print("usage: reaper svc ls [site|all] [component...] | svc logs [site] <name> [-n N] [--since T] [--host BANG]\n"
              "       reaper svc restart [site] <component|unit|container> [--host BANG] [--yes]")
        return 2
    if argv[0] == "restart":
        from reapercli.commands import svc_restart
        return svc_restart.run(argv[1:], ctx)
    return ls(argv[1:], ctx) if argv[0] == "ls" else logs(argv[1:], ctx)
