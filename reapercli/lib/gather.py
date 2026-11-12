"""Probe every site in scope (in parallel) and turn facts into rows."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from reapercli.lib.components import load_components, placed_on
from reapercli.lib.ha import ha_rows
from reapercli.lib.health import component_rows, host_rows, unmanaged_rows
from reapercli.lib.inventory import image_pins, site_hosts
from reapercli.lib.probe import probe_site
from reapercli.lib.sites import load_sites
from reapercli.lib.stale import stale_rows

LANE_ORDER = ("ha", "host", "infra", "core", "triage", "post-loose", "post-rack", "stale-bind", "unmanaged")


@dataclass
class SiteView:
    site: object
    hosts: object
    facts: dict
    comps: dict
    pins: dict
    vip_holder: object
    placed: dict


def _one(site, ctx, comps, toolchain):
    sh = site_hosts(site, ctx.root, ctx.hostname)
    facts = probe_site(sh, comps, runner=ctx.runner, toolchain=toolchain)
    holders = [b for b, f in facts.items() if f.get("vip")]
    placed = {c.name: placed_on(c, sh, site, ctx.root) for c in comps.values()}
    return SiteView(site, sh, facts, comps, image_pins(site, ctx.root),
                    holders[0] if len(holders) == 1 else None, placed)


def gather(sites: list, ctx, toolchain: bool = False) -> list:
    comps = load_components(ctx.root, load_sites(ctx.root))
    with ThreadPoolExecutor(max_workers=max(1, len(sites))) as pool:
        return list(pool.map(lambda s: _one(s, ctx, comps, toolchain), sites))


def site_rows(v: SiteView) -> list:
    rows = list(ha_rows(v.hosts, v.facts))
    for b in v.hosts.bangs:
        f = v.facts.get(b.name, {"error": "no facts"})
        peers = {x.name: x.dormant for x in v.hosts.bangs if x.name != b.name}
        rows += host_rows(b.name, f, peers)
        rows += component_rows(b.name, f, v.comps, v.vip_holder, v.pins, v.placed)
        rows += stale_rows(b.name, f)
        rows += unmanaged_rows(b.name, f, v.comps)
    rows = [replace(r, site=v.site.name) for r in rows]
    return sorted(rows, key=lambda r: LANE_ORDER.index(r.section) if r.section in LANE_ORDER else 99)
