"""Triage deploy steps (Phase 5; operator decisions 2026-11-08).

The triage repo's own compose is the only one. Its images are built ONCE, on the site's
primary, from the operator's clean, pushed clone (~/git/triage), tagged
prod-<svc>:<12-char sha>, and shipped to the standby; a site's differences go into the
.env reaper renders. Fetched files go to a temp dir outside every repo tree."""
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from reapercli.lib.guards import Refused
from reapercli.lib.images import on_primary, run_at
from reapercli.lib.inventory import ssh_target

CLONE = "~/git/triage"
DIST = "monitor/web/dist"
COMPOSE = "docker-compose-prod.yml"
FILES = (COMPOSE, ".env", ".env_prod")
PROJECT = "prod"
BUILD_TIMEOUT = 1800
_REQUIRED = re.compile(r"\$\{(\w+):?\?")
_SHA_TAG = re.compile(r"[0-9a-f]{12}")
PRUNE_KEEP = 1             # previous builds kept per service besides the live one (a --triage-ref rollback)


@dataclass(frozen=True)
class Source:
    sha: str
    tag: str
    branch: str
    target: Optional[str]      # the operator's ssh target for the primary; None = here
    ref: bool = False          # an older commit (--triage-ref): never built from HEAD's tree
    note: str = ""             # a warn line for the plan (UI dist older than the UI source)


def _state(sh, ctx, branch: str, ref: Optional[str]) -> tuple:
    target = on_primary(sh, ctx.hostname, ctx.user)
    b = shlex.quote(f"origin/{branch}")
    cmd = (f"cd {CLONE} || exit 9; "
           # compare with origin as it is now, not as of the clone's last fetch
           f"timeout 20 git fetch -q origin {shlex.quote(branch)} >/dev/null 2>&1 "
           f"&& echo fetched=1 || echo fetched=0; echo branch=$(git branch --show-current); "
           f"echo dirty=$(git status --porcelain | wc -l); echo head=$(git rev-parse HEAD); "
           f"git merge-base --is-ancestor HEAD {b} && echo pushed=1 || echo pushed=0; "
           f"echo behind=$(git rev-list --count HEAD..{b}); "
           # the UI is gitignored (monitor/web/dist) but COPYed into the api image: it must
           # exist, and the plan says when it predates the last UI commit
           f"test -f {DIST}/index.html && echo dist=$(stat -c %Y {DIST}/index.html); "
           f"echo uisrc=$(git log -1 --format=%ct -- monitor/web)")
    if ref:
        r = shlex.quote(ref + "^{commit}")
        cmd += (f"; echo ref=$(git rev-parse --verify {r}); "
                f"git merge-base --is-ancestor {r} {b} && echo refok=1 || echo refok=0")
    res = run_at(target, cmd, 30, ctx.runner)
    st = dict(l.split("=", 1) for l in res.out.splitlines() if "=" in l)
    if res.rc or "head" not in st:
        raise Refused(f"{sh.primary} {CLONE}: unreadable ({(res.err.strip() or 'rc %d' % res.rc)[-200:]})")
    return target, st


def guard(sh, ctx, branch: str = "main", ref: Optional[str] = None) -> Source:
    """The clone_guard rules, on the primary's triage clone."""
    target, st = _state(sh, ctx, branch, ref)
    where, head = f"{sh.primary} {CLONE}", st["head"]
    if st.get("dirty", "0").strip() != "0":
        raise Refused(f"{where}: clone dirty: {st['dirty'].strip()} paths")
    if st.get("branch") != branch:
        raise Refused(f"{where}: branch {st.get('branch') or '(detached)'} is not {branch}: "
                      f"pass --triage-branch {st.get('branch')} to use it deliberately")
    if st.get("pushed") != "1":
        raise Refused(f"{where}: HEAD {head[:7]} not on origin/{branch}: push first")
    if st.get("behind", "0").strip() not in ("", "0"):
        raise Refused(f"{where}: HEAD {head[:7]} is {st['behind']} commits behind origin/{branch}: git pull first")
    if ref:
        if not st.get("ref", "").strip():
            raise Refused(f"{where}: cannot resolve {ref} to one commit")
        if st.get("refok") != "1":
            raise Refused(f"{where}: {ref} not on origin/{branch}")
        return Source(st["ref"], st["ref"][:12], branch, target, True)
    if not st.get("dist", "").strip():
        raise Refused(f"{where}: no {DIST}/index.html: build the UI first (the api image COPYs it)")
    notes = []
    if st.get("fetched") != "1":
        notes.append(f"  warn could not fetch origin/{branch}: compared with the clone's last fetch")
    if int(st["dist"]) < int(st.get("uisrc") or 0):
        notes.append(f"  warn {DIST} is older than the last monitor/web commit: "
                     f"rebuild the UI if that commit changed it")
    return Source(head, head[:12], branch, target, False, "\n".join(notes))


def fetch(src: Source, ctx, dest: Path) -> dict:
    """FILES at src.sha (git show: the commit, not the work tree), each written 0600."""
    got = {}
    for name in FILES:
        r = run_at(src.target, f"cd {CLONE} && git show {src.sha}:{shlex.quote(name)}", 30, ctx.runner)
        if r.rc:
            raise Refused(f"{CLONE} {src.sha[:7]}: cannot read {name}: {r.err.strip()[-200:]}")
        fd = os.open(dest / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(r.out)
        got[name] = r.out
    if "${TRIAGE_TAG" not in got[COMPOSE]:
        raise Refused(f"triage {src.sha[:7]}: {COMPOSE} has no ${{TRIAGE_TAG}} image tags "
                      f"(the reaper-deploy change is not in that commit)")
    return got


def required_vars(compose: str) -> set:
    # YAML comments (line start or after whitespace) are never interpolated by compose
    return set(_REQUIRED.findall(re.sub(r"(^|\s)#.*$", r"\1", compose, flags=re.M)))


def render_env(repo_env: str, tag: str, site_env: dict) -> str:
    lines = [l for l in repo_env.splitlines() if l.strip()]
    lines.append(f"TRIAGE_TAG={tag}")
    lines += [f"{k}={site_env[k]}" for k in sorted(site_env)]
    return "\n".join(lines) + "\n"


def parse_env(text: str) -> dict:
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.lstrip().startswith("#"))


def check_env(compose: str, env_text: str) -> None:
    missing = sorted(required_vars(compose) - set(parse_env(env_text)))
    if missing:
        raise Refused("triage .env does not set " + ", ".join(missing)
                      + ": add them to apply_triage_compose_env in the site's group_vars")


def images(comp, tag: str) -> list:
    return [f"{PROJECT}-{s}:{tag}" for s in comp.compose.services]


def missing(target, imgs: list, runner) -> list:
    cmd = ("for i in " + " ".join(shlex.quote(i) for i in imgs)
           + '; do docker image inspect "$i" >/dev/null 2>&1 || echo "$i"; done')
    r = run_at(target, cmd, 30, runner)
    if r.rc:
        raise Refused(f"cannot list images on {target or 'here'}: {r.err.strip()[-200:]}")
    return [l.strip() for l in r.out.splitlines() if l.strip()]


def build_cmd(env: dict) -> str:
    pre = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"cd {CLONE} && {pre} docker compose -p {PROJECT} -f {COMPOSE} build"


def ship_cmd(imgs: list, standby_ip: str) -> str:
    load = shlex.quote("gunzip | docker load")
    return (f"set -o pipefail; docker save {' '.join(map(shlex.quote, imgs))} | gzip -1 | "
            f"ssh -o BatchMode=yes -o ConnectTimeout=6 flax@{standby_ip} {load}")


def verify_cmd() -> str:
    return (f"docker ps --filter label=com.docker.compose.project={PROJECT} "
            "--format '{{.Names}} {{.Image}} {{.State}}'")


def verify(out: str, comp, tag: str) -> list:
    seen = {p[0]: p[1:] for p in (l.split() for l in out.splitlines()) if len(p) == 3}
    bad = []
    for s in comp.compose.services:
        name, want = f"{PROJECT}-{s}-1", f"{PROJECT}-{s}:{tag}"
        if name not in seen:
            bad.append(f"{name}: not running")
        elif seen[name][0] != want:
            bad.append(f"{name}: runs {seen[name][0]}, want {want}")
        elif seen[name][1] != "running":         # a restart loop is listed too
            bad.append(f"{name}: {seen[name][1]}, not running")
    return bad


def standby_cmd() -> str:
    """What notify_master would start, resolved the way it runs it (from /, no chdir)."""
    return f"cd / && docker compose -p {PROJECT} -f /etc/flax/triage/{COMPOSE} config --images"


def standby_problems(out: str, imgs: list) -> list:
    got = [l.strip() for l in out.splitlines() if l.strip()]
    bad = [f"staged compose resolves {g}, want {w}" for g in got for w in imgs
           if g.split(":")[0] == w.split(":")[0] and g != w]
    bad += [f"staged compose does not name {w}" for w in imgs if w.split(":")[0] not in {g.split(":")[0] for g in got}]
    return bad


def prepare(sh, imgs: list, ctx, src: Source, env: dict, yes: bool) -> list:
    """Plan lines; with `yes`, build on the primary and ship to each live standby lacking
    the images. Refused when a live bang still lacks one: notify_master would then try
    to build in /etc/flax/triage, which has no source."""
    lines = []
    if missing(src.target, imgs, ctx.runner):
        if src.ref:
            raise Refused(f"{sh.primary}: images for triage {src.tag} are gone: check it out and deploy")
        lines.append(f"  build on {sh.primary}: {CLONE} {src.tag} ({len(imgs)} images)")
        if yes:
            r = run_at(src.target, build_cmd(env), BUILD_TIMEOUT, ctx.runner)
            if r.rc or missing(src.target, imgs, ctx.runner):
                tail = "\n".join((r.out + r.err).splitlines()[-20:])
                raise Refused(f"{sh.primary}: triage build failed (rc {r.rc})\n{tail}")
    else:
        lines.append(f"  images present on {sh.primary}: prod-*:{src.tag}")
    for b in sh.bangs:
        if b.name == sh.primary:
            continue
        if b.dormant:
            lines.append(f"  warn ship {b.name}: dormant, not shipped")
            continue
        flax = ssh_target(b, sh.on_site)
        if not missing(flax, imgs, ctx.runner):
            lines.append(f"  ship {b.name}: images present")
            continue
        lines.append(f"  ship {b.name} ({b.mgmt_ip}): prod-*:{src.tag}")
        if yes:
            r = run_at(src.target, ship_cmd(imgs, b.mgmt_ip), BUILD_TIMEOUT, ctx.runner)
            left = missing(flax, imgs, ctx.runner)
            if r.rc or left:
                raise Refused(f"{b.name}: ship failed, still missing {' '.join(left) or '?'}: "
                              f"{r.err.strip()[-200:]}")
    return lines


@dataclass
class Prepared:
    src: Source
    env: dict          # what the build sees: TRIAGE_TAG + the site's lists
    imgs: list
    dir: Path          # the fetched compose + rendered .env + .env_prod (apply_triage_compose_src)
    lines: list        # plan lines


def site_env(site) -> dict:
    from reapercli.lib.inventory import _group_vars
    raw = _group_vars(site, "all").get("apply_triage_compose_env") or {}
    bad = sorted(str(k) for k, v in raw.items() if not isinstance(v, str))
    if bad:     # `KEY:` with no value is a typo, not an empty list: never blank a DISABLE list
        raise Refused(f"{site.name}: apply_triage_compose_env {', '.join(bad)} must be a quoted string "
                      f"(\"\" for none)")
    return {str(k): v for k, v in raw.items()}


def setup(site, sh, comp, ctx, workdir: Path, branch: str = "main", ref: Optional[str] = None) -> Prepared:
    """Guard, fetch, render and check; then the read-only half of prepare (plan lines)."""
    src = guard(sh, ctx, branch, ref)
    got = fetch(src, ctx, workdir)
    senv = site_env(site)
    text = render_env(got[".env"], src.tag, senv)
    check_env(got[COMPOSE], text)
    fd = os.open(workdir / ".env", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    env = {"TRIAGE_TAG": src.tag, **senv}
    imgs = images(comp, src.tag)
    lines = [f"  triage {sh.primary} {CLONE}: {src.sha[:7]} ({branch}{', --triage-ref' if ref else ''})"
             f" -> prod-*:{src.tag}"] + ([src.note] if src.note else [])
    return Prepared(src, env, imgs, workdir, lines + prepare(sh, imgs, ctx, src, env, False))


def check_running(v, comp, p: Prepared, ctx) -> list:
    """Problem lines from the VIP holder's prod containers after the play."""
    b = next(b for b in v.hosts.bangs if b.name == v.vip_holder)
    r = run_at(ssh_target(b, v.hosts.on_site), verify_cmd(), 30, ctx.runner)
    bad = ([f"{b.name}: docker ps failed: {r.err.strip()[-200:]}"] if r.rc
           else [f"{b.name}: {x}" for x in verify(r.out, comp, p.src.tag)])
    for o in v.hosts.bangs:            # a failover must start the same images
        if o.name == b.name or o.dormant:
            continue
        r = run_at(ssh_target(o, v.hosts.on_site), standby_cmd(), 30, ctx.runner)
        bad += ([f"{o.name}: compose config failed: {r.err.strip()[-200:]}"] if r.rc
                else [f"{o.name}: {x}" for x in standby_problems(r.out, p.imgs)])
    return bad


def prune_list_cmd() -> str:
    return "docker images --filter 'reference=prod-*' --format '{{.Repository}} {{.Tag}} {{.CreatedAt}}'"


def stale_images(out: str, comp, tag: str, keep: int = PRUNE_KEEP) -> list:
    """Commit-tagged builds of comp's services beyond the live tag and the `keep` newest
    others. rollback-*, latest and other repos are never touched."""
    repos = {f"{PROJECT}-{s}" for s in comp.compose.services}
    by = {}
    for line in out.splitlines():
        p = line.split(None, 2)
        if len(p) == 3 and p[0] in repos and _SHA_TAG.fullmatch(p[1]) and p[1] != tag:
            by.setdefault(p[0], []).append((p[2], p[1]))
    return sorted(f"{r}:{t}" for r, rows in by.items() for _, t in sorted(rows, reverse=True)[keep:])


def prune(v, comp, tag: str, ctx) -> list:
    """After a verified deploy: drop old builds on every live bang. Lines; never fatal."""
    lines = []
    for b in v.hosts.bangs:
        if b.dormant:
            continue
        t = ssh_target(b, v.hosts.on_site)
        r = run_at(t, prune_list_cmd(), 30, ctx.runner)
        gone = [] if r.rc else stale_images(r.out, comp, tag)
        if r.rc:
            lines.append(f"  warn prune {b.name}: docker images failed: {r.err.strip()[-200:]}")
        elif gone:
            r = run_at(t, "docker rmi " + " ".join(shlex.quote(g) for g in gone), 300, ctx.runner)
            lines.append(f"  warn prune {b.name}: docker rmi failed: {r.err.strip()[-200:]}" if r.rc
                         else f"  pruned {b.name}: {len(gone)} old prod-* images")
    return lines
