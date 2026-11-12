"""reaper fw [site] bmc|bios <family> <artifact> [--yes] (spec 2026-11-11-reaper-fw-design).
reaper fw [site] [status [bmc|bios] [<family>]]: read-only, see commands/fw_status.py.

Move one product family's firmware target at a site, from a tar on the share:
read the tar's MANIFEST on the VIP holder (the version is never typed), copy the tar
to the standby bang to bang, rewrite the family's inventory entry (bios: and the linked
post BIOS entry), commit + push, render the manifests (their handlers restart the post
and console readers), then drain + stop the triage agent, give the family's rows a
fresh evaluation (fwreset_remote.py, triage's own rules) and recreate the agent.
Without --yes: the plan only. Every step is skipped when already done, so a run that
stopped half way is finished by running it again.
"""
import argparse
import copy
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Optional

import yaml

from reapercli.lib import fwentry, fwsteps
from reapercli.lib.ansible import run_playbook
from reapercli.lib.components import load_components
from reapercli.lib.fwdrain import DRAIN_TIMEOUT, manual_release, off_cmd
from reapercli.lib.guards import Refused, clone_guard
from reapercli.lib.hapath import _MAPPED, _interrupt, _set, standby_dormant, vip_holder
from reapercli.lib.inventory import _Loader, site_hosts
from reapercli.lib.sites import ScopeError, load_sites, resolve_scope

POST_PLAYBOOK = "apply_bang_services.yml"
POST_TAG = "post_bios_manifest"


@dataclass
class FwPlan:
    site: object
    sh: object
    lane: str
    family: str
    rel: str
    holder: str
    standby: Optional[str]
    standby_ip: Optional[str]
    tar: object
    version: str
    old_target: str
    vars_path: object
    old_text: str
    new_text: str
    post_families: list
    env: dict
    rendered: dict
    post_rendered: dict
    rows: list
    standby_sha: Optional[str]
    drift: list = field(default_factory=list)
    stale: dict = field(default_factory=dict)
    needs: set = field(default_factory=set)
    notes: list = field(default_factory=list)


def _holder(sh, runner) -> str:
    holder = vip_holder(sh, runner)
    if holder:
        return holder
    if not sh.standby or standby_dormant(sh):
        return sh.primary
    raise Refused(f"{sh.site}: no single VIP holder; run: reaper ha status {sh.site}")


def _post_families(text: str, family: str) -> list:
    items = (yaml.load(text, Loader=_Loader) or {}).get("post_bios_fw_platforms") or []
    return [e.get("family") for e in items if isinstance(e, dict) and e.get("host_fw_family") == family]


def _new_text(text: str, lane: str, family: str, version: str, rel: str) -> str:
    lp = fwentry.LANES[lane]
    e = fwentry.entry(text, lp.var, family)
    new = text
    if e.get("target_version") != version or e["flash"].get("artifact") != rel:
        new = fwentry.rewrite_entry(text, lp.var, family, version, rel)
    if lane == "bios":
        items = (yaml.load(new, Loader=_Loader) or {}).get("post_bios_fw_platforms") or []
        if any(isinstance(i, dict) and i.get("host_fw_family") == family
               and (i.get("target") != version or i.get("bin_url") != f"{fwentry.SHARE_URL}/{rel}")
               for i in items):
            new, _ = fwentry.rewrite_post_bios(new, family, version, rel)
    return new


def plan(site, lane: str, family: str, artifact: str, ctx) -> FwPlan:
    lp = fwentry.LANES[lane]
    rel = fwsteps.artifact_rel(artifact)
    vars_path = site.inventory / "group_vars" / "all" / "vars.yml"
    text = vars_path.read_text()
    old = fwentry.entry(text, lp.var, family)          # refuses unknown / report-only families
    sh = site_hosts(site, ctx.root, ctx.hostname)
    holder = _holder(sh, ctx.runner)
    tar = fwsteps.read_tar(sh, holder, rel, ctx.runner)
    m = fwentry.parse_manifest(tar.manifest_text)
    fwentry.check_lane(lane, m["purpose"])
    version = m["version"]
    new_text = _new_text(text, lane, family, version, rel)

    live = [b for b in sh.bangs if not b.dormant]
    others = [b for b in live if b.name != holder]
    standby = others[0] if others else None
    standby_sha = fwsteps.remote_sha(sh, standby.name, rel, ctx.runner) if standby else None
    env = fwsteps.agent_env(sh, holder, lp.agent, ctx.runner)
    rendered = {b.name: fwsteps.rendered_target(sh, b.name, lane, family, ctx.runner) for b in live}
    post_fams = _post_families(new_text, family) if lane == "bios" else []
    post_rendered = ({b.name: fwsteps.post_bios_targets(sh, b.name, ctx.runner) for b in live}
                     if post_fams else {})

    p = FwPlan(site, sh, lane, family, rel, holder, standby.name if standby else None,
               standby.mgmt_ip if standby else None, tar, version, old.get("target_version", ""),
               vars_path, text, new_text, post_fams, env, rendered, post_rendered, [], standby_sha)
    if holder != sh.primary:
        p.notes.append(f"VIP on {holder}, inventory primary {sh.primary}")
    if standby and standby_sha != tar.sha256:
        p.needs.add("sync")
    if new_text != text:
        p.needs.add("inventory")
    if any(t != version for t in rendered.values()):
        p.needs.add("render")
    if any(tg.get(f) != version for tg in post_rendered.values() for f in post_fams):
        p.needs.add("post_render")
    if lane == "bios" and not post_fams:
        p.notes.append(f"post BIOS not linked to {family}: post not updated")
    p.stale = {b.name: fwsteps.stale(fwsteps.reader_ages(sh, b.name, lane, ctx.runner), lane) for b in live}
    if "agent" in p.stale.get(holder, []):
        p.needs.add("agent")
    if any(n != "agent" for v in p.stale.values() for n in v):
        p.needs.add("readers")
    if env.get("running"):
        comp = load_components(ctx.root, load_sites(ctx.root))["triage"]
        p.drift = fwsteps.env_drift(env, fwsteps.compose_service(sh, holder, comp, lp.agent, ctx.runner))

    if env.get("running"):
        new_manifest = copy.deepcopy((yaml.load(new_text, Loader=_Loader) or {})[lp.var])
        spec = _spec(p, apply=False)
        spec["manifest_json"] = new_manifest
        p.rows = fwsteps.clean_rows(sh, holder, spec, ctx.runner)["rows"]
        if any(r["action"] in ("delete", "repark") for r in p.rows):
            p.needs.add("clean")
    else:
        p.notes.append(f"{lp.agent} is not running on {holder}: no rows to clean")
    return p


def _spec(p: FwPlan, apply: bool, ports=None) -> dict:
    lp = fwentry.LANES[p.lane]
    s = {"lane": p.lane, "family": p.family, "new_target": p.version, "manifest": lp.manifest,
         "store": lp.store, "apply": apply, "feed_url": p.env.get("SWITCHPORTRECON_URL")}
    if ports is not None:
        s["ports"] = ports
    return s


def _mode_line(p: FwPlan) -> str:
    e = p.env
    if not e.get("running"):
        return f"{fwentry.LANES[p.lane].agent}: not running on {p.holder}"
    enable = e.get("ENABLE_PORTS", "")
    return (f"{fwentry.LANES[p.lane].agent} on {p.holder}: mode {e.get('MODE', '?')}, "
            f"{'ENABLE_PORTS (empty: every port)' if not enable else 'ENABLE_PORTS ' + enable}, "
            f"DISABLE_PORTS {e.get('DISABLE_PORTS') or '(none)'}")


def show(p: FwPlan) -> None:
    s = p.site.name
    print(f"fw {s}: {p.lane} {p.family} {p.old_target} -> {p.version}   tar {p.rel} (sha256 {p.tar.sha256[:12]})")
    for n in p.notes:
        print(f"note  {n}")
    print(f"note  {_mode_line(p)}")
    for d in p.drift:
        print(f"WARN  {fwsteps.API.split('-')[0]}-{fwentry.LANES[p.lane].agent} compose drift: {d} (--yes refuses)")
    if p.lane == "bmc":
        print("note  post's BMC target moves too (flax-post-fwd reads the same manifest)")
    steps = [("sync", f"{s}/{p.holder:<12} copy the tar to {p.standby}"),
             ("inventory", f"{s}  commit + push {p.vars_path.relative_to(p.vars_path.parents[4])}"
                           + (f" (+ post BIOS {', '.join(p.post_families)})" if p.post_families else "")),
             ("render", f"{s}  ansible {fwentry.LANES[p.lane].playbook}"),
             ("post_render", f"{s}  ansible {POST_PLAYBOOK} --tags {POST_TAG} (restarts flax-post-biosd)"),
             ("readers", f"{s}  restart readers started before the manifest: "
                         + (", ".join(f"{b}:{n}" for b, v in p.stale.items() for n in v if n != "agent") or "none")),
             ("clean", f"{s}/{p.holder:<12} drain + stop {fwentry.LANES[p.lane].agent}, clean rows, recreate")]
    agent_due = bool(p.needs & {"clean", "agent", "render", "inventory"})
    for key, line in steps:
        if key == "post_render" and not p.post_families:
            continue
        due = agent_due if key == "clean" else key in p.needs
        print(f"{'plan ' if due else 'done '} {line}")
    for r in p.rows:
        if r["action"] != "keep" or r["reason"] != "other family":
            print(f"  {r['port']:<8} {r['phase']:<18} {r['target'] or '-':<22} {r['action']:<7} {r['reason']}")


def _git(root, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _commit_msg(p: FwPlan) -> str:
    lines = [f"fw({p.site.name}): {p.family} {p.old_target} -> {p.version}", "", f"artifact: {p.rel}",
             f"sha256: {p.tar.sha256}"]
    if p.post_families:
        lines.append(f"post BIOS: {', '.join(p.post_families)} follows")
    return "\n".join(lines + ["", "Generated-by: reaper fw"]) + "\n"


def _one(p: FwPlan, bang: str, cmd: str, ctx) -> int:
    timeout = DRAIN_TIMEOUT if "fwdrain.py on" in cmd else 300
    b = next(x for x in p.sh.bangs if x.name == bang)
    from reapercli.lib.inventory import ssh_target
    from reapercli.lib.remote import fanout
    r = fanout({bang: ssh_target(b, p.sh.on_site)}, cmd, None, timeout, ctx.runner)[bang]
    print(f"{p.site.name}/{bang:<12} {'ok' if r.rc == 0 else 'FAIL'}  {cmd}")
    if r.rc:
        print((r.err or r.out).strip())
    return r.rc


def _start(p: FwPlan, comp, agent: str, ctx) -> int:
    if _one(p, p.holder, fwsteps.agent_start_cmd(comp, agent), ctx):
        print(f"  {agent} is DOWN: reaper svc restart {p.site.name} prod-{agent}-1 --yes")
        print(f"  and release the drain: {manual_release(p.holder, [agent])}")
        return 1
    e = fwsteps.agent_env(p.sh, p.holder, agent, ctx.runner)
    if not e.get("running"):
        print(f"{p.site.name}/{p.holder:<12} FAIL  {agent} not running after the recreate")
        return 1
    print(f"{p.site.name}/{p.holder:<12} ok    {agent} running on {p.holder}: mode {e.get('MODE', '?')}, "
          f"ENABLE_PORTS {e.get('ENABLE_PORTS') or '(empty: every port)'}, "
          f"DISABLE_PORTS {e.get('DISABLE_PORTS') or '(none)'}")
    return 0


def _clean(p: FwPlan, ctx) -> int:
    """drain -> stop -> row clean -> recreate. Once the drain is asked for, the finally
    block ALWAYS releases it or recreates the agent, also on Ctrl-C / SIGHUP / SIGTERM."""
    lp = fwentry.LANES[p.lane]
    comp = load_components(ctx.root, load_sites(ctx.root))["triage"]
    drift = fwsteps.env_drift(fwsteps.agent_env(p.sh, p.holder, lp.agent, ctx.runner),
                              fwsteps.compose_service(p.sh, p.holder, comp, lp.agent, ctx.runner))
    if drift:
        print(f"{p.site.name}/{p.holder:<12} FAIL  compose drift, {lp.agent} left as it is: " + "; ".join(drift))
        return 1
    drain, stop = fwsteps.agent_stop_cmds(comp, lp.agent)
    rc, result, stop_tried = 0, None, False
    old = _set({s: _interrupt for s in _MAPPED})
    try:
        if _one(p, p.holder, drain, ctx):
            return 1
        stop_tried = True                      # before the call: a timed-out stop may still have stopped it
        if _one(p, p.holder, stop, ctx):
            return 1
        try:
            result = fwsteps.clean_rows(p.sh, p.holder, dict(_spec(p, apply=True),
                                                             deadline_s=fwsteps.CLEAN_DEADLINE), ctx.runner)
        except Exception as e:                 # noqa: BLE001 -- whatever it was, the agent comes back
            print(f"{p.site.name}/{p.holder:<12} FAIL  {e}")
            rc = 1
    finally:
        shield = _set({s: signal.SIG_IGN for s in (signal.SIGINT, *_MAPPED)})
        try:
            if stop_tried:
                rc |= _start(p, comp, lp.agent, ctx)
            elif _one(p, p.holder, off_cmd(comp, [lp.agent]), ctx):
                print(f"  run: {manual_release(p.holder, [lp.agent])}")
        finally:
            _set(shield)
            _set(old)
    if result is None:
        return rc or 1
    done = [r for r in result["rows"] if r["action"] in ("delete", "repark")]
    for r in result["rows"]:
        if r["action"] != "keep" or r["reason"] != "other family":
            print(f"  {r['port']:<8} {r['phase']:<18} {r['action']:<7} {r['reason']}")
    print(f"{p.site.name}/{p.holder:<12} ok    {len(done)} rows reset; backup {result['backup'] or '(none needed)'}")
    deleted = [r["port"] for r in done if r["action"] == "delete"]
    if p.standby and deleted:
        image = fwsteps.api_image(p.sh, p.holder, ctx.runner)
        if fwsteps.agent_env(p.sh, p.standby, lp.agent, ctx.runner).get("running"):
            print(f"note  {lp.agent} is running on the standby {p.standby}: its store left alone")
        elif not image:
            print(f"note  no api image on {p.holder}: standby store not cleaned")
        else:
            try:
                s = fwsteps.clean_rows(p.sh, p.standby, _spec(p, apply=True, ports=deleted), ctx.runner,
                                       image=image)
                n = len([r for r in s["rows"] if r["action"] == "delete"])
                print(f"{p.site.name}/{p.standby:<12} ok    standby store: {n} rows reset; "
                      f"backup {s['backup'] or '(none needed)'}")
            except Refused as e:
                print(f"{p.site.name}/{p.standby:<12} FAIL  {e}")
                rc = 1
    return rc


def _restart_readers(p: FwPlan, ctx) -> int:
    """Post units / the console that started before the manifest they read: restart them."""
    rc = 0
    for b in [x.name for x in p.sh.bangs if not x.dormant]:
        for n in fwsteps.stale(fwsteps.reader_ages(p.sh, b, p.lane, ctx.runner), p.lane):
            if n == "agent":
                continue
            cmd = f"docker restart {fwsteps.CONSOLE}" if n == "console" else f"sudo -n systemctl restart {n}"
            rc |= 1 if _one(p, b, cmd, ctx) else 0
    return rc


def apply(p: FwPlan, ctx) -> int:
    if not p.needs:
        print(f"fw {p.site.name}: {p.family} already at {p.version}: nothing to do")
        return 0
    root = ctx.clone or ctx.root
    if "sync" in p.needs:
        if fwsteps.sync(p.sh, p.holder, p.standby_ip, p.rel, ctx.runner) or \
                fwsteps.remote_sha(p.sh, p.standby, p.rel, ctx.runner) != p.tar.sha256:
            print(f"{p.site.name}/{p.holder:<12} FAIL  tar copy to {p.standby} (sha256 differs or copy failed)")
            return 1
        print(f"{p.site.name}/{p.holder:<12} ok    tar copied to {p.standby}")
    if "inventory" in p.needs:
        p.vars_path.write_text(p.new_text)
        rel_path = str(p.vars_path.relative_to(root))
        r = _git(root, "commit", "-q", "-m", _commit_msg(p), "--", rel_path)
        if r.returncode:
            print(f"FAIL  git commit: {r.stderr.strip()}")
            return 1
        sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
        branch = _git(root, "branch", "--show-current").stdout.strip()
        if _git(root, "push", "-q", "origin", branch).returncode:
            print(f"FAIL  git push: commit {sha} is local, not pushed; nothing was changed on the bangs")
            return 1
        print(f"{p.site.name}  ok    commit {sha} pushed")
    if "render" in p.needs or "inventory" in p.needs:
        if run_playbook(p.site, ctx.root, fwentry.LANES[p.lane].playbook, runner=ctx.play_runner):
            print(f"FAIL  {fwentry.LANES[p.lane].playbook}")
            return 1
    if p.post_families and ("post_render" in p.needs or "inventory" in p.needs):
        if run_playbook(p.site, ctx.root, POST_PLAYBOOK, tags=(POST_TAG,), runner=ctx.play_runner):
            print(f"FAIL  {POST_PLAYBOOK} --tags {POST_TAG}")
            return 1
    bad = {b: t for b in p.rendered
           if (t := fwsteps.rendered_target(p.sh, b, p.lane, p.family, ctx.runner)) != p.version}
    if bad:
        print(f"FAIL  rendered manifest still says {bad}: agent left alone")
        return 1
    if _restart_readers(p, ctx):
        return 1
    agent_stale = "agent" in fwsteps.stale(fwsteps.reader_ages(p.sh, p.holder, p.lane, ctx.runner), p.lane)
    if p.env.get("running") and ("clean" in p.needs or agent_stale):
        return _clean(p, ctx)
    return 0


def run(argv: list, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    if not rest or rest[0] == "status":
        from reapercli.commands import fw_status
        return fw_status.run(sites, rest[1:], ctx)
    ap = argparse.ArgumentParser(prog="reaper fw")
    ap.add_argument("lane", choices=sorted(fwentry.LANES))
    ap.add_argument("family")
    ap.add_argument("artifact", help="/export/share/<path>.tar on the VIP holder, or <path>.tar")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(rest)
    if len(sites) != 1:
        raise ScopeError("reaper fw: name one site")
    if a.yes:
        clone_guard(ctx.clone or ctx.root)
    p = plan(sites[0], a.lane, a.family, a.artifact, ctx)
    show(p)
    if a.yes and p.drift:
        raise Refused(f"{fwentry.LANES[p.lane].agent}: the triage compose differs from the running agent, and the "
                      "recreate would apply it: " + "; ".join(p.drift) + " (recreate it on purpose first: "
                      f"reaper svc restart {p.site.name} prod-{fwentry.LANES[p.lane].agent}-1)")
    if not p.needs:
        print(f"fw {p.site.name}: {p.family} already at {p.version}: nothing to do")
        return 0
    if not a.yes:
        if p.needs:
            try:
                clone_guard(ctx.clone or ctx.root)
            except Refused as e:
                print(f"note  --yes would refuse: {e}")
        print("(plan only: add --yes to run)")
        return 0
    return apply(p, ctx)
