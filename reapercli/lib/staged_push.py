"""reaper push: switch config, and the staged push for management-critical changes
(a change on a link a jump host uses to reach the switch). No prompts: each stage is
its own `--stage N --yes` run, and stage 1 records what it checked, so stages 2 and 3
apply exactly that plan or refuse.

  stage 1  pre-flight VLAN check, then the stage-1 jump host gets its new config
  stage 2  the switches, via the stage-3 jump host; probe stage1 -> primary turtle
  stage 3  the stage-3 jump host (skipped when the site has one jump host)"""
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from reapercli.lib.guards import Refused
from reapercli.lib.inventory import _yaml, site_hosts
from reapercli.lib.remote import fanout


def state_path(home: Path, site: str) -> Path:
    return home / ".cache" / "reaper" / f"staged-push-{site}.json"


def tree_hash(configs: Path, site: str) -> str:
    """Everything a stage reads: every device's desired config, topology.yml and
    services/. authorized_keys is excluded because apply_jumphost rewrites it itself."""
    h = hashlib.sha256()
    base = configs / site
    files = [*base.glob("*/config/**/*"), base / "topology.yml", *base.glob("services/**/*")]
    for p in sorted(files):
        if p.is_file() and p.name != "authorized_keys":
            h.update(str(p.relative_to(base)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


@dataclass
class Plan:
    site: str
    limit: str
    critical: list
    tree: str
    stage1_jh: str
    stage3_jh: str
    single: bool
    done: list = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=1))

    @classmethod
    def load(cls, path: Path) -> "Plan":
        return cls(**json.loads(path.read_text()))


def preflight(site_dir: Path, changes: Path) -> int:
    script = Path(__file__).resolve().parents[2] / "scripts" / "preflight_check.py"
    return subprocess.run([sys.executable, str(script), str(site_dir), str(changes)]).returncode


def _jump(site, ctx, name) -> str:
    """flax@<bang name>, on site too: the name resolves through reaper's ssh config to the
    tailscale address, never the mgmt VLAN path a staged change may have just cut."""
    sh = site_hosts(site, ctx.root, ctx.hostname)
    if not name or name not in {b.name for b in sh.bangs}:
        raise Refused(f"{site.name}: topology jump host {name!r} is not a bang in the inventory; reaper fetch {site.name}")
    if name in site.dormant_hosts:
        raise Refused(f"{site.name}: topology jump host {name} is dormant")
    return f"flax@{name}"


def _device_address(site, name: str) -> str:
    def find(group):
        for h, v in ((group or {}).get("hosts") or {}).items():
            if h == name:
                return (v or {}).get("ansible_host")
        for child in ((group or {}).get("children") or {}).values():
            a = find(child)
            if a:
                return a
    addr = find(_yaml(site.inventory / "hosts.yml").get("all"))
    if not addr or "{{" in str(addr):
        raise Refused(f"{site.name}: no plain ansible_host for {name} in the inventory: cannot probe it")
    return str(addr)


def probe(site, ctx, jump: str, turtle: str) -> bool:
    """From the stage-1 bang itself, open the turtle's ssh port over the mgmt VLAN: the
    path the staged change is about. (raw/echo over network_cli runs on the controller.)"""
    addr = _device_address(site, turtle)
    r = fanout({turtle: jump}, f"timeout 8 bash -c '</dev/tcp/{addr}/22'", None, 30, ctx.runner)[turtle]
    return r.rc == 0


def _topology(site, ctx) -> dict:
    from reapercli.commands.net import configs_dir
    p = configs_dir(ctx) / site.name / "topology.yml"
    if not p.exists():
        raise Refused(f"{site.name}: no {p}: run reaper fetch {site.name} first")
    return yaml.safe_load(p.read_text()) or {}


def _cmd(site, limit: str, n: int) -> str:
    return f"reaper push {site.name}{' ' + limit.replace(':', ' ') if limit else ''} --stage {n} --yes"


def _switches(site, ctx, limit: str, yes: bool, out) -> int:
    from reapercli.commands.net import check_switches, critical_changes, play
    if check_switches(site, ctx, limit, diff=False) != 0:
        return 1
    crit = critical_changes(ctx, site)
    if not crit:
        if not yes:
            out(f"{site.name}: no management-critical changes; rerun with --yes to apply")
            return 0
        return play(site, ctx, "apply_config.yml", limit=limit or None)
    out(f"\n{site.name}: STAGED PUSH REQUIRED: management-critical changes")
    for c in crit:
        out(f"  {c.get('device', '?')}  {c.get('port', '?')}  "
            f"(peer: {c.get('peer_device', '?')}/{c.get('peer_port', '?')})")
    out("run, in order:")
    for n in (1, 2, 3):
        out(f"  {_cmd(site, limit, n)}")
    if yes:
        raise Refused(f"{site.name}: management-critical changes: run --stage 1 --yes, then 2, then 3")
    return 0


def _stage1(site, ctx, limit: str, yes: bool, out) -> int:
    from reapercli.commands.net import check_switches, configs_dir, critical_changes, play
    topo = _topology(site, ctx)           # refuse before any check run, as bash did
    if check_switches(site, ctx, limit, diff=False) != 0:
        return 1
    crit = critical_changes(ctx, site)
    if not crit:
        raise Refused(f"{site.name}: no staged push needed (no management-critical changes): "
                      f"reaper push {site.name} --yes")
    site_dir = configs_dir(ctx) / site.name
    if preflight(site_dir, site_dir / ".critical_changes.json") != 0:
        out(f"FAIL {site.name}: pre-flight VLAN check")
        return 1
    pairs = topo.get("jumphost_pairs") or {}
    s1, s3 = pairs.get("stage1"), pairs.get("stage3")
    for jh in (s1, s3) if s3 else (s1,):
        _jump(site, ctx, jh)
    plan = Plan(site.name, limit, crit, tree_hash(configs_dir(ctx), site.name), s1, s3 or s1, not s3)
    out(f"stage 1 {site.name}: apply_jumphost_config.yml to {s1} (it loses the switch until stage 2)")
    if plan.single:
        out(f"  single jump host: stage 2 pushes the switch via {s1}; safe only if {s1} has no VLAN change")
    if not yes:
        out("plan only: rerun with --yes")
        return 0
    path = state_path(ctx.home, site.name)
    plan.save(path)
    if play(site, ctx, "apply_jumphost_config.yml", limit=s1) != 0:
        out(f"FAIL {site.name}: stage 1 apply on {s1}")
        return 1
    plan.done.append(1)
    plan.save(path)
    out(f"stage 1 done. next: {_cmd(site, limit, 2)}")
    return 0


def _recorded(n: int, site, ctx, limit: str) -> Plan:
    from reapercli.commands.net import configs_dir
    path = state_path(ctx.home, site.name)
    if not path.exists():
        raise Refused(f"{site.name}: no stage 1 recorded: {_cmd(site, limit, 1)}")
    plan = Plan.load(path)
    if plan.limit != limit:
        raise Refused(f"{site.name}: stage 1 was for devices '{plan.limit or 'all'}', not '{limit or 'all'}'")
    if plan.tree != tree_hash(configs_dir(ctx), site.name):
        raise Refused(f"{site.name}: config files changed since stage 1; start over: {_cmd(site, limit, 1)}")
    if n - 1 not in plan.done:
        raise Refused(f"{site.name}: stage {n - 1} not done: {_cmd(site, limit, n - 1)}")
    return plan


def _stage2(site, ctx, limit: str, yes: bool, out) -> int:
    from reapercli.commands.net import configs_dir, play
    plan = _recorded(2, site, ctx, limit)
    via, probe_from = _jump(site, ctx, plan.stage3_jh), _jump(site, ctx, plan.stage1_jh)
    turtle = _topology(site, ctx).get("primary_turtle")
    if not turtle:
        raise Refused(f"{site.name}: topology.yml has no primary_turtle to probe: reaper fetch {site.name}")
    _device_address(site, turtle)       # refuse now, not after the switches changed
    target = limit or "all:!jumphosts:!localhost"     # every switch, whatever its group
    out(f"stage 2 {site.name}: apply_config.yml to {target} via {plan.stage3_jh}; "
        f"probe {plan.stage1_jh} -> {turtle}")
    if not yes:
        out("plan only: rerun with --yes")
        return 0
    proxy = f"ssh -W %h:%p -o StrictHostKeyChecking=no {via}"
    rc = play(site, ctx, "apply_config.yml", limit=target, extra={
        "ansible_ssh_extra_args": f"-o ProxyCommand='{proxy}'",
        "ansible_paramiko_proxy_command": proxy})      # cisco is paramiko: it ignores the ssh args
    if rc != 0:
        out(f"FAIL {site.name}: stage 2 apply via {plan.stage3_jh}; stage 1 stays recorded, rerun stage 2")
        return 1
    path = state_path(ctx.home, site.name)
    if not probe(site, ctx, probe_from, turtle):
        path.unlink(missing_ok=True)
        out(f"FAIL {site.name}: probe {plan.stage1_jh} -> {turtle} failed.")
        out(f"  The switches have the NEW config (applied via {plan.stage3_jh}) and it is NOT reverted;")
        out(f"  {plan.stage1_jh} has its new config too. {plan.stage3_jh} should still reach the switches.")
        out("  After a clean pre-flight this means a physical or forwarding problem. To roll back, restore")
        out(f"  the previous files under files/configs/{site.name}/ from git and re-apply by hand.")
        return 1
    plan.done.append(2)
    if plan.single:
        path.unlink(missing_ok=True)
        out("stage 3 skipped (single jump host). switch config applied; management path intact.")
        return 0
    plan.save(path)
    out(f"stage 2 done. next: {_cmd(site, limit, 3)}")
    return 0


def _stage3(site, ctx, limit: str, yes: bool, out) -> int:
    from reapercli.commands.net import play
    plan = _recorded(3, site, ctx, limit)
    out(f"stage 3 {site.name}: apply_jumphost_config.yml to {plan.stage3_jh}")
    if not yes:
        out("plan only: rerun with --yes")
        return 0
    if play(site, ctx, "apply_jumphost_config.yml", limit=plan.stage3_jh) != 0:
        out(f"FAIL {site.name}: stage 3 apply on {plan.stage3_jh}; rerun stage 3")
        return 1
    state_path(ctx.home, site.name).unlink(missing_ok=True)
    out("stage 3 done: both jump hosts and the switches agree on the new config.")
    return 0


def stage(n: int, site, ctx, limit: str, yes: bool = True, out=print) -> int:
    return {1: _stage1, 2: _stage2, 3: _stage3}[n](site, ctx, limit, yes, out)


def push(site, ctx, devices: list, yes: bool, stage_n=None, out=print) -> int:
    from reapercli.commands.net import play, split_devices
    jh, sw = split_devices(devices)
    if stage_n:
        if jh:
            raise Refused("--stage is for switches; push jump hosts on their own")
        return stage(stage_n, site, ctx, sw, yes, out)
    rc = 0
    if not devices or sw:
        rc |= _switches(site, ctx, sw, yes, out)
    if not devices or jh:
        rc |= play(site, ctx, "apply_jumphost_config.yml", check=not yes, limit=jh or None)
    return rc
