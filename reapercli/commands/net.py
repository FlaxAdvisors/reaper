"""reaper net fetch|drift|plan|push|setup [site|all] [device...] (spec §6): the switch and
jump-host config commands, also accepted bare (`reaper push …`). setup also installs the
clone's rule-zero git hooks and the user's shell environment (scripts/shell-env.sh). No prompts: push
applies only with --yes, and management-critical changes go one --stage at a time
(lib/staged_push.py)."""
import argparse
import json
import subprocess
from pathlib import Path

from reapercli.lib.ansible import run_playbook
from reapercli.lib.guards import Refused
from reapercli.lib.sites import ScopeError, resolve_scope

SUBS = ("fetch", "drift", "plan", "push", "setup")
USAGE = ("usage: reaper net fetch | drift | plan | push | setup [site|all] [device...] [--yes] [--stage N]\n"
         "  (fetch, drift, plan, push and setup are also accepted without `net`)")


def configs_dir(ctx) -> Path:
    return ctx.configs or ctx.root / "files" / "configs"


def play(site, ctx, playbook: str, **kw) -> int:
    return run_playbook(site, ctx.root, playbook, runner=ctx.play_runner, configs=configs_dir(ctx), **kw)


def split_devices(devices: list) -> tuple:
    """(jump-host limit, switch limit), each `:`-joined; bang-* names are jump hosts."""
    jh = [d for d in devices if d.startswith("bang-")]
    sw = [d for d in devices if not d.startswith("bang-")]
    return ":".join(jh), ":".join(sw)


def check_switches(site, ctx, limit: str, diff: bool) -> int:
    """apply_config.yml in check mode, its critical-change analysis from THIS run only:
    the analysis play (localhost) reads every <device>/.check_result.json, so stale ones
    go first, and a device limit gets localhost added or the analysis would not run."""
    site_dir = configs_dir(ctx) / site.name
    (site_dir / ".critical_changes.json").unlink(missing_ok=True)
    for p in site_dir.glob("*/.check_result.json"):
        p.unlink()
    return play(site, ctx, "apply_config.yml", check=True, diff=diff,
                limit=f"{limit}:localhost" if limit else None)


def critical_changes(ctx, site) -> list:
    p = configs_dir(ctx) / site.name / ".critical_changes.json"
    if not p.exists():
        raise RuntimeError(f"{p} not written by apply_config.yml")
    return json.loads(p.read_text())


def _plan(site, ctx, devices: list) -> int:
    jh, sw = split_devices(devices)
    rc = 0
    if not devices or sw:
        rc |= check_switches(site, ctx, sw, diff=True)
    if not devices or jh:
        rc |= play(site, ctx, "apply_jumphost_config.yml", check=True, limit=jh or None)
    return rc


def install_hooks(ctx) -> int:
    """Rule zero in the tooling: pre-commit + pre-push -> scripts/check-secrets, in the
    clone reaper runs from (ctx.clone; tests inject a scratch one)."""
    clone = ctx.clone or ctx.root
    rc = subprocess.run([str(ctx.root / "scripts" / "install-git-hooks.sh"), str(clone)]).returncode
    if rc == 0:
        print(f"git hooks: pre-commit + pre-push -> scripts/check-secrets ({clone})")
    return rc


def shell_env(ctx) -> int:
    """The running user's shell: reaper on PATH, ~/.bashrc (completion, pdsh, python3.12),
    ~/jumphosts.txt -- scripts/shell-env.sh, for ctx.home and the clone reaper runs from."""
    import os
    clone = ctx.clone or ctx.root
    import sys
    env = dict(os.environ, HOME=str(ctx.home))
    env.setdefault("TOOLPY", sys.executable)        # the toolchain's python3.12
    return subprocess.run([str(ctx.root / "scripts" / "shell-env.sh"), str(clone)], env=env).returncode


def _setup(sites, ctx) -> int:
    hooks_rc = install_hooks(ctx)
    out = ctx.home / ".ssh" / "config.d" / "reaper-jumphosts"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.parent.chmod(0o700)
    rc = 0
    for site in sites:
        print(f"==> setup: {site.name}")
        rc |= play(site, ctx, "render_ssh_jumphosts.yml", extra={"output_file": str(out)})
    print(f"Written: {out}")
    env_rc = shell_env(ctx)
    return 1 if rc or hooks_rc or env_rc else 0


def run(argv: list, ctx) -> int:
    if not argv or argv[0] not in SUBS:
        print(USAGE)
        return 2
    sub = argv[0]
    sites, rest = resolve_scope(argv[1:], ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog=f"reaper net {sub}")
    if sub != "setup":
        p.add_argument("devices", nargs="*")
    if sub == "push":
        p.add_argument("--yes", action="store_true")
        p.add_argument("--stage", type=int, choices=(1, 2, 3))
    a = p.parse_args(rest)
    devices = getattr(a, "devices", [])
    if devices and len(sites) != 1:
        raise ScopeError(f"device names need one site: reaper {sub} <site> {' '.join(devices)}")
    if sub == "setup":
        return _setup(sites, ctx)
    rc, refused = 0, []
    for site in sites:
        if len(sites) > 1:
            print(f"\n==> site: {site.name}")
        limit = ":".join(devices) or None
        if sub in ("fetch", "drift"):
            r = play(site, ctx, "collect_config.yml", check=sub == "drift", limit=limit)
        elif sub == "plan":
            r = _plan(site, ctx, devices)
        else:
            from reapercli.lib.staged_push import push
            try:
                r = push(site, ctx, devices, a.yes, a.stage)
            except Refused as e:
                if len(sites) == 1:
                    raise
                print(f"reaper: refused: {e}")      # one site's refusal must not skip the others
                refused.append(site.name)
                continue
        if r:
            print(f"FAIL: {site.name}")
            rc = 1
    if refused:
        raise Refused(f"refused for {', '.join(refused)} (see above); the other sites ran")
    return rc
