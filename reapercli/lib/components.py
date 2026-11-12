"""The component registry (files/reaper/components.yml, spec §5.2).

One entry per thing reaper deploys. Every unit, container, timer, cron file and
port a role installs is listed under exactly one component; `report` enumerates
from here, so anything installed but not listed is a registry bug.
"""
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from reapercli.lib.inventory import group_override

LANES = ("infra", "core", "triage", "post-loose", "post-rack")
PLACEMENTS = ("primary", "standby", "both")
KINDS = ("unit", "compose", "host")
_LISTS = ("tags", "units", "containers", "timers", "crons", "files", "ports", "udp_ports", "binds", "norestart")


@dataclass(frozen=True)
class Compose:
    """How to drive a compose project: always from its dir, always with its env file."""
    dir: str
    project: str
    file: str                       # basename inside dir
    env_file: Optional[str] = None  # basename inside dir; compose reads only .env on its own
    services: tuple = ()            # what `svc restart` may recreate (never postgres)
    drain: Optional[dict] = field(default=None, hash=False, compare=False)


def _compose(name: str, e: Optional[dict]) -> Optional[Compose]:
    if not e:
        return None
    drain = e.get("drain")
    if drain:
        drain = {"services": tuple(drain["services"]), "cmd": drain["cmd"], "release": drain["release"]}
    return Compose(e["dir"], e["project"], e["file"], e.get("env_file"),
                   tuple(e.get("services") or ()), drain)


def compose_argv(c: Compose, *args: str) -> str:
    env = f" --env-file {shlex.quote(c.env_file)}" if c.env_file else ""
    tail = " ".join(shlex.quote(a) for a in args)
    return (f"cd {shlex.quote(c.dir)} && docker compose{env} -p {shlex.quote(c.project)}"
            f" -f {shlex.quote(c.file)} {tail}")


@dataclass(frozen=True)
class Component:
    name: str
    lane: str
    tags: tuple
    placement: str
    kind: str
    placement_var: Optional[str] = None
    units: tuple = ()
    containers: tuple = ()
    timers: tuple = ()
    crons: tuple = ()
    files: tuple = ()
    ports: tuple = ()             # TCP listeners
    udp_ports: tuple = ()         # UDP listeners (the site health board checks both)
    binds: tuple = ()
    norestart: tuple = ()         # units `svc restart` refuses (flax-stack bounces postgres)
    compose: Optional[Compose] = field(default=None, hash=False, compare=False)
    image_var: Optional[str] = None
    image_repo: Optional[str] = None   # only containers of this image are compared to image_var's pin
    ha_sensitive: bool = False
    vip_gated: bool = False       # runs only on the VIP holder
    optional: bool = False        # site-dependent: absent is info, never FAIL
    group: Optional[str] = None   # composite deploy name, e.g. flax_core
    playbook: str = "apply_bang_services.yml"
    freshness: Optional[dict] = field(default=None, hash=False, compare=False)


def load_components(root: Path, site_names) -> dict:
    raw = yaml.safe_load((root / "files" / "reaper" / "components.yml").read_text()) or {}
    reserved = set(site_names) | {"all"}
    comps = {}
    for name, e in raw.items():
        if name in reserved:
            raise ValueError(f"component {name!r} collides with a site name or 'all'")
        for key, allowed in (("lane", LANES), ("placement", PLACEMENTS), ("kind", KINDS)):
            if e.get(key) not in allowed:
                raise ValueError(f"component {name}: {key} {e.get(key)!r} not in {allowed}")
        if e["kind"] == "compose" and not e.get("compose"):
            raise ValueError(f"component {name}: kind compose needs a compose: block")
        kw = {k: tuple(e.get(k) or ()) for k in _LISTS}
        kw["compose"] = _compose(name, e.get("compose"))
        rest = {k: v for k, v in e.items() if k not in _LISTS and k != "compose"}
        comps[name] = Component(name=name, **{**rest, **kw})
    return comps


def placed_on(c: Component, sh, site, root: Path) -> tuple:
    names = tuple(b.name for b in sh.bangs)
    if c.placement_var:
        override = group_override(site, root, c.placement_var)
        if override is not None:
            return tuple(n for n in names if n in override)
    if c.placement == "primary":
        return (sh.primary,)
    if c.placement == "standby":
        return sh.standby[:1] or (sh.primary,)
    return names


def expected_running(c: Component, bang: str, placed: tuple, vip_holder) -> bool:
    return bang in placed and (not c.vip_gated or bang == vip_holder)
