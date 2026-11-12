"""reaper deploy [site|all] <component…> [--yes] [--branch B] [--restart-stale] (spec §8).

The registry gives the tags; the play runs against BOTH bangs (the roles VIP-gate the
standby, so a failover starts the deployed version) with no --limit except a dormant
host's. ha_sensitive components go through the HA path. Afterwards: a deploy record
per component on every live bang, then the stale bind-mount check."""
import argparse
import shutil
import signal
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from reapercli.lib import fwdrain, triage
from reapercli.lib.ansible import run_playbook
from reapercli.lib.components import load_components
from reapercli.lib.gather import gather
from reapercli.lib.guards import Refused, clone_guard, missing_images
from reapercli.lib.hapath import _MAPPED, _interrupt, _set, ha_guard, standby_dormant
from reapercli.lib.out import render
from reapercli.lib.record import make_record, write_records
from reapercli.lib.sites import ScopeError, load_sites, resolve_scope
from reapercli.lib.stale import restart_hint, stale_binds, stale_rows


def expand(names: list, comps: dict) -> list:
    out = []
    for n in names:
        if n == "keepalived":
            raise Refused("keepalived deploys only via ha keepalived-roll (Phase 3): master first")
        members = [c for c in comps.values() if c.group == n] or ([comps[n]] if n in comps else [])
        if not members:
            raise ScopeError(f"deploy: {n!r} is not a component or group")
        out += [c for c in members if c not in out]
    return out


def play_groups(comps: list) -> list:
    groups = {}
    for c in comps:
        tags = groups.setdefault(c.playbook, [])
        tags += [t for t in c.tags if t not in tags]
    return [(pb, tuple(tags)) for pb, tags in groups.items()]


def _image(c, pins):
    return f"{c.image_repo}:{pins[c.image_var]}" if c.image_repo and c.image_var in pins else None


def _plan(v, comps, head, branch, tri=None):
    bangs = " ".join(b.name + (" (dormant)" if b.dormant else "") for b in v.hosts.bangs)
    lines = [f"deploy {v.site.name}: {bangs}   commit {head[:7]} ({branch})"]
    lines += [f"  {pb}" + (f" --tags {','.join(tags)}" if tags else "") for pb, tags in play_groups(comps)]
    ha = any(c.ha_sensitive for c in comps)
    lines.append("  HA path: " + ("skipped (standby dormant)" if ha and standby_dormant(v.hosts)
                                  else "yes" if ha else "no"))
    lines += [f"  image {i}" for i in sorted({_image(c, v.pins) for c in comps} - {None})]
    lines += tri.lines if tri else []
    lines += [f"  drain {bang}: {' '.join(agents)} (released after)" for bang, _, agents in fwdrain.targets(v, comps)]
    return "\n".join(lines)


def _vip_gate(v, comps):
    """A VIP-gated compose role (apply_triage_compose) brings its stack up on the DECLARED
    master and downs it on the other bang: with the VIP elsewhere that kills the live stack."""
    for c in comps:
        if c.kind == "compose" and c.vip_gated and v.vip_holder != v.hosts.primary:
            raise Refused(f"{v.site.name}: VIP is on {v.vip_holder or 'no single bang'}, not the declared "
                          f"master {v.hosts.primary}; deploying {c.name} would stop its live stack there. "
                          f"Fail back first.")


# --check: only what would change. ok/skipped hosts are not printed; no_log tasks keep their
# values hidden. Every task that ignores check mode only reads or honours DRY (audited 2026-11-10).
CHECK_ENV = {"ANSIBLE_DISPLAY_OK_HOSTS": "false", "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "false"}


def _check(v, comps, ctx) -> int:
    print(f"{v.site.name}: check mode -- nothing is changed; shell/command steps are not simulated",
          flush=True)
    rc = 0
    for pb, tags in play_groups(comps):
        r = run_playbook(v.site, ctx.root, pb, tags=tags, runner=ctx.play_runner, check=True, diff=True,
                         env_extra=CHECK_ENV)
        if r:
            print(f"{v.site.name}: FAIL check {pb} rc={r}")
            rc = r
    return rc


def _plays(v, comps, ctx, extra=None) -> int:
    for pb, tags in play_groups(comps):
        rc = run_playbook(v.site, ctx.root, pb, tags=tags, runner=ctx.play_runner, extra=extra)
        if rc:
            print(f"{v.site.name}: FAIL {pb} rc={rc}")
            return rc
    return 0


def _stale(site, ctx, restart: bool) -> int:
    from reapercli.commands.svc_restart import restart as svc_restart
    v = gather([site], ctx)[0]
    pairs = [(b.name, name) for b in v.hosts.bangs for name, _ in stale_binds(v.facts.get(b.name, {}))]
    if not pairs:
        return 0
    rows = [r for b in v.hosts.bangs for r in stale_rows(b.name, v.facts.get(b.name, {}))]
    print(render([r.__class__(**{**r.__dict__, "site": site.name}) for r in rows]))
    if not restart:
        for bang, name in dict.fromkeys(pairs):
            print("  run: " + restart_hint(site.name, bang, name))
        return 1
    for bang, name in dict.fromkeys(pairs):
        svc_restart(site, name, bang, ctx, yes=True)
    v = gather([site], ctx)[0]
    left = [(b.name, n) for b in v.hosts.bangs for n, _ in stale_binds(v.facts.get(b.name, {}))]
    for bang, name in left:
        print(f"{site.name}/{bang}: FAIL still stale after restart: {name}")
    return 1 if left else 0


def _guarded_plays(v, comps, ctx, extra=None) -> int:
    if any(c.ha_sensitive for c in comps):
        with ha_guard(v.site, v.hosts, v.vip_holder, ctx) as g:
            rc = _plays(v, comps, ctx, extra)
        return rc or (0 if g.ok else 1)
    return _plays(v, comps, ctx, extra)


def _drained_plays(v, comps, ctx, extra=None) -> int:
    """Drain the running fw agents, play, and ALWAYS release what was drained (fwdrain)."""
    from reapercli.commands.svc_restart import _run
    held, todo = [], fwdrain.targets(v, comps)
    old = _set({s: _interrupt for s in _MAPPED}) if todo else None   # a dropped session still releases
    try:
        for bang, c, agents in todo:
            held.append((bang, c, agents))      # before the call: a timed-out `on` leaves it set
            if _run(v.hosts, bang, fwdrain.on_cmd(c, agents), ctx.runner):
                print(f"{v.site.name}/{bang}: FAIL drain; nothing deployed")
                return 1
        return _guarded_plays(v, comps, ctx, extra)
    finally:
        if held:
            shield = _set({s: signal.SIG_IGN for s in (signal.SIGINT, *_MAPPED)})
            try:
                for bang, c, agents in held:
                    if _run(v.hosts, bang, fwdrain.off_cmd(c, agents), ctx.runner):
                        print(f"{v.site.name}/{bang}: FAIL release; run: {fwdrain.manual_release(bang, agents)}")
            finally:
                _set(shield)
        if old:
            _set(old)


def _triage_comp(comps):
    return next((c for c in comps if c.name == "triage"), None)


def _site(v, comps, head, a, ctx, tri=None) -> int:
    extra = None
    if tri:     # build on the primary + ship to the standby before anything is drained
        print(f"{v.site.name}: building/shipping triage {tri.src.tag} (up to 30 min)", flush=True)
        for line in triage.prepare(v.hosts, tri.imgs, ctx, tri.src, tri.env, True):
            print(line, flush=True)
        extra = {"apply_triage_compose_src": str(tri.dir)}
        v = gather([v.site], ctx)[0]      # the build is long: the VIP and the agents may have moved
        _vip_gate(v, comps)
    if _drained_plays(v, comps, ctx, extra):
        return 1
    if tri:
        bad = triage.check_running(v, _triage_comp(comps), tri, ctx)
        for line in bad:
            print(f"{v.site.name}/{line.split(':', 1)[0]}: FAIL {line.split(': ', 1)[1]}")
        if bad:
            return 1
        for line in triage.prune(v, _triage_comp(comps), tri.src.tag, ctx):
            print(f"{v.site.name}:{line}")
    now = datetime.now(timezone.utc)
    recs = []
    for c in comps:
        if tri and c.name == "triage":
            recs.append(make_record(c.name, head, f"prod-*:{tri.src.tag}", ctx.user, ctx.hostname, now,
                                    extra={"triage": tri.src.sha}))
        else:
            recs.append(make_record(c.name, head, _image(c, v.pins), ctx.user, ctx.hostname, now))
    live = [b.name for b in v.hosts.bangs if not b.dormant]
    for bang, r in write_records(v.hosts, live, recs, ctx.runner).items():
        if r.rc:
            print(f"{v.site.name}/{bang}: warn deploy record not written: {r.err.strip()}")
    return _stale(v.site, ctx, a.restart_stale)


def run(argv: list, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog="reaper deploy")
    p.add_argument("names", nargs="+", metavar="component")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--check", action="store_true", help="dry run: show the files and tasks that would change")
    p.add_argument("--branch")
    p.add_argument("--restart-stale", action="store_true")
    p.add_argument("--triage-branch", default="main")
    p.add_argument("--triage-ref")
    a = p.parse_args(rest)
    comps = expand(a.names, load_components(ctx.root, load_sites(ctx.root)))
    tcomp = _triage_comp(comps)
    if a.check and a.yes:
        raise ScopeError("deploy: --check or --yes, not both")
    if a.check and tcomp:
        raise ScopeError("deploy: --check does not cover triage (it builds and ships images)")
    if not tcomp and (a.triage_ref or a.triage_branch != "main"):
        raise ScopeError("deploy: --triage-branch/--triage-ref only with triage")
    head = clone_guard(ctx.clone or ctx.root, a.branch)
    views = gather(sites, ctx)
    missing = [f"{v.site.name}/{m}" for v in views for m in missing_images(v, comps)]
    if missing:
        raise Refused("image missing: " + "; ".join(missing) + " -- run: reaper image ship <site> <tag>")
    for v in views:
        _vip_gate(v, comps)
    tri, dirs = {}, []
    # a kill or a dropped ssh session (SIGTERM/SIGHUP) must still run the finally below
    term = _set({s: _interrupt for s in _MAPPED})
    try:
        for v in views:
            if tcomp:      # outside every repo tree: holds the fetched files and the site .env
                dirs.append(Path(tempfile.mkdtemp(prefix="reaper-triage-")))
                tri[v.site.name] = triage.setup(v.site, v.hosts, tcomp, ctx, dirs[-1],
                                                a.triage_branch, a.triage_ref)
        for v in views:
            print(_plan(v, comps, head, a.branch or "main", tri.get(v.site.name)))
        if a.check:
            rc = 0
            for v in views:
                rc |= _check(v, comps, ctx)
            return 1 if rc else 0
        if not a.yes:
            print("(plan only: --check shows what would change, --yes deploys)")
            return 0
        rc = 0
        for v in views:      # one site's refusal must not stop the sites after it
            try:
                rc |= _site(v, comps, head, a, ctx, tri.get(v.site.name))
            except Refused as e:
                print(f"{v.site.name}: refused: {e}", flush=True)
                rc |= 3
        return rc
    finally:
        _set(term)
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
