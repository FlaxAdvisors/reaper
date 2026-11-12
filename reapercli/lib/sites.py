"""Sites and scope (spec §4, §5.1).

`reaper <sub> [site|all] [args]`: the first argument is a scope only if it names
a site or is `all`. With no scope, a command covers every active site whose vault
password is on this machine -- a bang therefore defaults to its own site, the dev
VM to all of them.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml


class ScopeError(Exception):
    """A user-facing scope problem; main() prints it and exits 2."""


@dataclass(frozen=True)
class Site:
    name: str
    inventory: Path
    topology: Path
    state: str
    dormant_hosts: tuple


def load_sites(root: Path) -> dict:
    data = yaml.safe_load((root / "files" / "reaper" / "sites.yml").read_text())["sites"]
    sites = {
        name: Site(name, root / e["inventory"], root / e["topology"], e.get("state", "active"),
                   tuple(e.get("dormant_hosts", ())))
        for name, e in data.items()
    }
    for d in sorted((root / "inventories").iterdir()):
        if d.is_dir() and d.name not in sites:
            raise ScopeError(f"inventories/{d.name} has no entry in files/reaper/sites.yml")
    for s in sites.values():
        if not s.inventory.is_dir():
            raise ScopeError(f"site {s.name}: inventory {s.inventory} does not exist")
    return sites


def _vault(home: Path, name: str) -> Path:
    return home / f".vault_pass_{name}"


def resolve_scope(argv: list, root: Path, home: Path) -> tuple:
    sites = load_sites(root)
    active = [s for s in sites.values() if s.state == "active"]
    first = argv[0] if argv else None
    if first in sites:
        site = sites[first]
        if site.state != "active":
            raise ScopeError(f"site {first} is {site.state} in files/reaper/sites.yml")
        if not _vault(home, first).exists():
            raise ScopeError(f"no vault password for {first} here: {_vault(home, first)} is missing")
        return [site], list(argv[1:])
    in_scope = [s for s in active if _vault(home, s.name).exists()]
    if not in_scope:
        raise ScopeError(f"no site in scope: no ~/.vault_pass_<site> for any of {', '.join(sorted(sites))}")
    return in_scope, list(argv[1:] if first == "all" else argv)
