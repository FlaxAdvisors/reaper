"""reaper image ls|build|ship [site|all] (spec §6).

ls: every pinned image on every bang; a pin missing on the standby is FAIL (the next
failover or compose recreate there stops with `No such image`, build-and-deploy §5).
build <tag>: each site's primary, sites in parallel. ship <tag>: within a site only."""
import argparse
from concurrent.futures import ThreadPoolExecutor

from reapercli.lib.gather import gather
from reapercli.lib.guards import Refused, clone_guard
from reapercli.lib.images import (BUILD_CMD, REMOTE_CLONE, inspect_cmd, on_primary, run_at, ship_cmd,
                                  version_tag)
from reapercli.lib.inventory import site_hosts, ssh_target
from reapercli.lib.out import Row, exit_code, render
from reapercli.lib.sites import resolve_scope

BUILD_TIMEOUT = 1800


def image_rows(v) -> list:
    wanted = sorted({f"{c.image_repo}:{v.pins[c.image_var]}" for c in v.comps.values()
                     if c.image_repo and c.image_var in v.pins})
    rows = []
    for b in v.hosts.bangs:
        f = v.facts.get(b.name, {})
        for img in wanted:
            if f.get("dormant"):
                rows.append(Row("image", img, "dormant", "dormant", b.name, v.site.name))
            elif "error" in f or not isinstance(f.get("images"), list):
                rows.append(Row("image", img, "FAIL", f.get("error") or "no image list", b.name, v.site.name))
            else:
                ok = img in f["images"]
                rows.append(Row("image", img, "ok" if ok else "FAIL", "present" if ok else "MISSING", b.name, v.site.name))
    return rows


def _args(prog, rest, branch=False):
    p = argparse.ArgumentParser(prog=prog)
    p.add_argument("tag")
    if branch:
        p.add_argument("--branch")
    p.add_argument("--yes", action="store_true")
    return p.parse_args(rest)


def _check_primary(site, sh, tag, head, ctx):
    target = on_primary(sh, ctx.hostname, ctx.user)
    if target is not None:      # a remote clone: it must be the controller's commit, clean
        r = run_at(target, f"cd {REMOTE_CLONE} && git rev-parse HEAD && git status --porcelain", 30, ctx.runner)
        lines = r.out.splitlines()
        if r.rc or not lines or lines[0].strip() != head or len(lines) > 1:
            got = lines[0][:7] if lines else "unreadable"
            raise Refused(f"{site.name}: {sh.primary} clone at {got}{' (dirty)' if len(lines) > 1 else ''}"
                          f" (want {head[:7]}): git pull there")
    if run_at(target, inspect_cmd(tag), 30, ctx.runner).rc == 0:
        raise Refused(f"{site.name}: {sh.primary} already has flax-control:{tag}: ship it, or bump flax_post/version.py")
    return target


def build(argv, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    a = _args("reaper image build", rest, branch=True)
    have = version_tag(ctx.root)
    if have != a.tag:
        raise Refused(f"flax_post/version.py is {have}, not {a.tag}")
    head = clone_guard(ctx.clone or ctx.root, a.branch)
    jobs = []
    for s in sites:
        sh = site_hosts(s, ctx.root, ctx.hostname)
        target = _check_primary(s, sh, a.tag, head, ctx)
        root = ctx.root if target is None else REMOTE_CLONE
        jobs.append((s, sh, target, BUILD_CMD.format(root=root, tag=a.tag)))
    for s, sh, target, cmd in jobs:
        print(f"build {s.name}/{sh.primary} {'(here)' if target is None else ''}: {cmd}")
    if not a.yes:
        print("(plan only: add --yes to build)")
        return 0
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda j: run_at(j[2], j[3], BUILD_TIMEOUT, ctx.runner), jobs))
    rc = 0
    for (s, sh, _, _), r in zip(jobs, results):
        print(f"--- {s.name}/{sh.primary}: {'ok' if r.rc == 0 else 'FAIL rc=%d' % r.rc}")
        print("\n".join((r.out + r.err).splitlines()[-20:]))
        rc |= 1 if r.rc else 0
    return rc


def ship(argv, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    a = _args("reaper image ship", rest)
    rows, jobs = [], []
    for s in sites:
        sh = site_hosts(s, ctx.root, ctx.hostname)
        src = on_primary(sh, ctx.hostname, ctx.user)
        if run_at(src, inspect_cmd(a.tag), 30, ctx.runner).rc != 0:
            raise Refused(f"{s.name}: {sh.primary} has no flax-control:{a.tag}: reaper image build {s.name} {a.tag}")
        for b in sh.bangs:
            if b.name == sh.primary:
                continue
            if b.dormant:
                rows.append(Row("image", f"flax-control:{a.tag}", "warn", "standby dormant, not shipped", b.name, s.name))
            elif run_at(ssh_target(b, sh.on_site), inspect_cmd(a.tag), 30, ctx.runner).rc == 0:
                rows.append(Row("image", f"flax-control:{a.tag}", "ok", "already present", b.name, s.name))
            else:
                jobs.append((s, sh, src, b))
                print(f"ship {s.name}: {sh.primary} -> {b.name} ({b.mgmt_ip}) flax-control:{a.tag}")
    if rows:
        print(render(rows))
    if not a.yes:
        print("(plan only: add --yes to ship)")
        return 0
    for s, sh, src, b in jobs:
        r = run_at(src, ship_cmd(a.tag, b.mgmt_ip), BUILD_TIMEOUT, ctx.runner)
        ok = r.rc == 0 and run_at(ssh_target(b, sh.on_site), inspect_cmd(a.tag), 30, ctx.runner).rc == 0
        rows.append(Row("image", f"flax-control:{a.tag}", "ok" if ok else "FAIL",
                        "shipped" if ok else (r.err.strip().splitlines() or ["not present after load"])[-1],
                        b.name, s.name))
        print(render(rows[-1:]))
    return exit_code(rows)


def run(argv: list, ctx) -> int:
    if not argv or argv[0] not in ("ls", "build", "ship"):
        print("usage: reaper image ls [site|all] | image build|ship [site|all] <tag> [--yes]")
        return 2
    if argv[0] == "build":
        return build(argv[1:], ctx)
    if argv[0] == "ship":
        return ship(argv[1:], ctx)
    sites, _ = resolve_scope(argv[1:], ctx.root, ctx.home)
    rows = [r for v in gather(sites, ctx) for r in image_rows(v)]
    print(render(rows))
    return exit_code(rows)
