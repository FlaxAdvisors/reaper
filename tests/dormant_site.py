"""A site whose standby is dormant, made by the test that needs one.

The real files/reaper/sites.yml has no dormant host since 2026-11-18 (braintree's
bang-siesta is a live standby again), so the dormant-behaviour tests mark it here."""
import dataclasses
from pathlib import Path

import yaml

from reapercli.lib import sites as _sites


def dormant(site, host="bang-siesta"):
    """`site` (a frozen Site) with `host` in dormant_hosts."""
    return dataclasses.replace(site, dormant_hosts=(host,))


def siesta_dormant(monkeypatch):
    """For main()-driven tests: resolve_scope hands out braintree with bang-siesta dormant."""
    real = _sites.load_sites

    def load(root):
        s = real(root)
        return {**s, "braintree": dormant(s["braintree"])}
    monkeypatch.setattr(_sites, "load_sites", load)


def dormant_model(model, host="bang-siesta"):
    """A sitemodel.SiteModel with `host` in state dormant (devices.yml `state: dormant`)."""
    return dataclasses.replace(model, devices=tuple(
        dataclasses.replace(d, state="dormant") if d.name == host else d for d in model.devices))


def dormant_tree(tmp_path, site="braintree", host="bang-siesta"):
    """For apply_health_compose renders: copies of sites.yml and <site>/devices.yml with
    `host` dormant in both, laid out so the role's own lookups (role_path/../../files/...)
    read them. Returns the role_path to render with."""
    root = Path(__file__).resolve().parents[1]
    sites = yaml.safe_load((root / "files/reaper/sites.yml").read_text())
    sites["sites"][site]["dormant_hosts"] = [host]
    devices = yaml.safe_load((root / f"files/configs/{site}/devices.yml").read_text())
    for d in devices["devices"]:
        if d["name"] == host:
            d["state"] = "dormant"
    for rel, doc in (("files/reaper/sites.yml", sites), (f"files/configs/{site}/devices.yml", devices)):
        out = tmp_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(doc, sort_keys=False))
    role = tmp_path / "roles/apply_health_compose"
    role.mkdir(parents=True)
    return str(role)
