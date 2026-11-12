"""reaper svc restart [site] <component|unit|container> [--host BANG] [--yes] (spec §6, §8.4).

unit/host kinds: systemctl restart where the component is expected to run (the VIP
holder when vip_gated). compose kind: a --no-deps --force-recreate of the service,
always with its env file. postgres and flax-stack.service are never restarted here:
that bounce is `reaper deploy <site> flax_stack`, on purpose."""
import argparse
import shlex
from typing import Optional

from reapercli.lib.components import compose_argv, expected_running, load_components, placed_on
from reapercli.lib.fwdrain import DRAIN_TIMEOUT, manual_release, off_cmd, on_cmd
from reapercli.lib.guards import Refused
from reapercli.lib.hapath import ha_guard, standby_dormant, vip_holder
from reapercli.lib.inventory import site_hosts, ssh_target
from reapercli.lib.remote import fanout
from reapercli.lib.sites import ScopeError, load_sites, resolve_scope

POSTGRES_HINT = "postgres restarts only with the stack: reaper deploy <site> flax_stack"
NORESTART_HINT = {
    "flax-stack": POSTGRES_HINT,
    "docker": "no live-restore: it kills postgres and flax-control on the VIP holder",
    "keepalived": "its notify scripts stop and relaunch the stacks: reaper ha keepalived-roll (Phase 3)",
    "firewalld": "a restart can wipe docker's chains: reaper deploy <site> triage_firewall",
}


def _compose_service(name: str, comps: dict):
    """A compose container name (or bare service name) -> (component, service)."""
    for c in comps.values():
        if not c.compose:
            continue
        p = c.compose.project
        for cont in c.containers:
            svc = cont[len(p) + 1:-2] if cont.startswith(p + "-") and cont.endswith("-1") else None
            if svc and name in (cont, svc):
                return c, svc
    return None


def _recreate(c, svc: str) -> list:
    if svc not in c.compose.services:
        hint = POSTGRES_HINT if svc == "postgres" else f"restartable: {', '.join(c.compose.services)}"
        raise Refused(f"{svc} ({c.name}) is not restartable here; {hint}")
    # root: the compose dirs/env files are root-only (/etc/flax/flax-stack is 0750, its env 0600)
    cmd = "sudo -n bash -c " + shlex.quote(compose_argv(c.compose, "up", "-d", "--no-deps", "--force-recreate", svc))
    drain = c.compose.drain
    # the drain is its own step (long timeout), and only that agent: bare `on` drains them all
    return [on_cmd(c, [svc]), cmd] if drain and svc in drain["services"] else [cmd]


def _unit(c, unit: str) -> str:
    if unit in c.norestart:
        raise Refused(f"{unit} is not restartable here: {NORESTART_HINT.get(unit, 'see components.yml')}")
    return f"sudo -n systemctl restart {shlex.quote(unit)}"


def _commands(name: str, comps: dict):
    from reapercli.commands.svc import resolve
    if name in comps:
        c = comps[name]
        if c.compose:
            return c, [x for s in c.compose.services for x in _recreate(c, s)]
        units = [u for u in c.units if u not in c.norestart]
        if not units and c.units:
            _unit(c, c.units[0])          # raises with that unit's reason
        if not units:
            raise Refused(f"{name} has no unit or compose service to restart")
        return c, [_unit(c, u) for u in units]
    found = resolve(name, comps)
    if found and found[0] == "unit":
        return found[2], [_unit(found[2], name)]
    cs = _compose_service(name, comps)
    if cs:
        return cs[0], _recreate(*cs)
    if found:
        raise Refused(f"{name} is a container of {found[2].name}: restart its unit")
    raise ScopeError(f"svc restart: {name!r} is not a registered component, unit or container")


def plan_restart(name: str, comps: dict, sh, site, root, holder: Optional[str],
                 host: Optional[str] = None):
    c, cmds = _commands(name, comps)
    if c.ha_sensitive and holder is None and not standby_dormant(sh):
        raise Refused(f"{c.name} is ha-sensitive and there is no single VIP holder: reaper ha status {sh.site}")
    if host:
        if host not in [b.name for b in sh.bangs]:
            raise ScopeError(f"--host {host} is not a bang of {sh.site}")
        if c.vip_gated and host != holder:
            raise Refused(f"{c.name} runs only on the VIP holder {holder}, not {host}")
        targets = [host]
    else:
        placed = placed_on(c, sh, site, root)
        targets = [b.name for b in sh.bangs if not b.dormant and expected_running(c, b.name, placed, holder)]
    if not targets:
        raise Refused(f"no bang runs {c.name} here (VIP holder {holder})")
    return c, [(b, cmd) for b in targets for cmd in cmds]


def _run(sh, bang: str, cmd: str, runner) -> int:
    b = next(x for x in sh.bangs if x.name == bang)
    timeout = DRAIN_TIMEOUT if "fwdrain.py on" in cmd else 300
    r = fanout({bang: ssh_target(b, sh.on_site)}, cmd, None, timeout, runner)[bang]
    print(f"{sh.site}/{bang:<12} {'ok' if r.rc == 0 else 'FAIL'}  {cmd}")
    if r.rc:
        print((r.err or r.out).strip())
    return r.rc


def restart(site, name: str, host: Optional[str], ctx, yes: bool) -> int:
    comps = load_components(ctx.root, load_sites(ctx.root))
    sh = site_hosts(site, ctx.root, ctx.hostname)
    holder = vip_holder(sh, ctx.runner)
    c, cmds = plan_restart(name, comps, sh, site, ctx.root, holder, host)
    if not yes:
        for b, cmd in cmds:
            print(f"plan  {site.name}/{b:<12} {cmd}")
        print("(plan only: add --yes to run)")
        return 0
    rc = 0
    guarded = [x for x in cmds if c.ha_sensitive and x[0] == holder]
    if guarded:
        with ha_guard(site, sh, holder, ctx) as g:
            rc |= _steps(sh, guarded, ctx.runner, c)
        rc |= 0 if g.ok else 1
    rc |= _steps(sh, [x for x in cmds if x not in guarded], ctx.runner, c)
    return 1 if rc else 0


def _steps(sh, cmds: list, runner, c=None) -> int:
    """In order; a bang's later steps (the recreate after its drain) stop at its first failure.
    A drained agent whose recreate never happened is released: nothing else would clear it."""
    failed, drained, rc = set(), {}, 0
    for b, cmd in cmds:
        if b in failed:
            print(f"{sh.site}/{b:<12} skip  {cmd}")
            continue
        if c is not None and c.compose and c.compose.drain and cmd.startswith(c.compose.drain["cmd"] + " "):
            drained[b] = cmd[len(c.compose.drain["cmd"]) + 1:].split()
        if _run(sh, b, cmd, runner):
            failed.add(b)
            rc = 1
    for b in failed & set(drained):
        if _run(sh, b, off_cmd(c, drained[b]), runner):
            print(f"  run: {manual_release(b, drained[b])}")
    return rc


def run(argv: list, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog="reaper svc restart")
    p.add_argument("name")
    p.add_argument("--host")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(rest)
    if len(sites) != 1:
        print("reaper svc restart: name one site")
        return 2
    return restart(sites[0], a.name, a.host, ctx, a.yes)
