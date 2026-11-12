"""Inventory facts from plain YAML -- no ansible, no vault (spec §3).

The HA pair is what the inventory declares: the primary is
apply_keepalived_master_host, the standby is every other jumphost. Whether the
live VIP agrees is the probe's business (lib/ha.py), not this module's.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Bang:
    name: str
    mgmt_ip: str
    dormant: bool


@dataclass(frozen=True)
class SiteHosts:
    site: str
    primary: str
    standby: tuple
    bangs: tuple
    on_site: bool
    vip: str = ""


class _Loader(yaml.SafeLoader):
    """Tolerates ansible tags (!vault, !unsafe) by reading them as plain scalars."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node)
                              if isinstance(node, yaml.ScalarNode) else None)


def _yaml(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=_Loader) if path.exists() else {}


def _group_vars(site, group: str) -> dict:
    return _yaml(site.inventory / "group_vars" / group / "vars.yml") or {}


def site_hosts(site, root: Path, hostname: str) -> SiteHosts:
    hosts = _yaml(site.inventory / "hosts.yml")
    names = tuple(hosts["all"]["children"]["jumphosts"]["hosts"])
    allvars = _group_vars(site, "all")
    primary = allvars["apply_keepalived_master_host"]
    suffix = allvars["bang_host_suffix"]
    bangs = tuple(Bang(n, f"192.168.88.{20 + int(suffix[n])}", n in site.dormant_hosts) for n in names)
    return SiteHosts(site.name, primary, tuple(n for n in names if n != primary), bangs,
                     hostname.split(".")[0] in names, str(allvars.get("bang_mgmt_vip", "")))


def ssh_target(bang: Bang, on_site: bool) -> str:
    """Always as flax, said on the command line: the operator's ssh config may name the
    bang with their own user (templates/ssh_jumphosts.j2 sets none)."""
    return f"flax@{bang.mgmt_ip}" if on_site else f"flax@{bang.name}"


def image_pins(site, root: Path) -> dict:
    return {k: str(v) for k, v in _group_vars(site, "all").items() if k.endswith("_image_tag")}


def group_override(site, root: Path, var: str):
    jumphosts = _group_vars(site, "jumphosts")
    if var not in jumphosts:
        return None
    v = jumphosts[var]
    return [v] if isinstance(v, str) else list(v or [])
