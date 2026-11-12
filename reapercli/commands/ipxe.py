"""reaper ipxe [site|all] [--cached] [--yes]: build + install the TFTP NBPs.

The NBPs (/srv/tftpboot/{efi64/ipxe.efi,bios/undionly.kpxe}) are never part of a deploy:
the build is not byte-reproducible, so a rebuild per pxe_payloads deploy replaced ipxe.efi
every time (operator 2026-11-10). This plays apply_bang_services.yml --tags tftp with
apply_pxe_payloads_ipxe_build + _install set: build on the builder bang (the pinned
IPXE_COMMIT in roles/apply_pxe_payloads/files/ipxe/ipxe.conf), fetch to the controller
cache, install on every live bang. --cached skips the build and installs the cache (one
tested build rolled to the other sites). Same clone guard, HA path and deploy record
(component "ipxe") as reaper deploy."""
import argparse
import dataclasses
import re
from datetime import datetime, timezone

from reapercli.commands.deploy import _guarded_plays
from reapercli.lib.components import load_components
from reapercli.lib.gather import gather
from reapercli.lib.guards import clone_guard
from reapercli.lib.record import make_record, write_records
from reapercli.lib.sites import load_sites, resolve_scope

_ROLE = "roles/apply_pxe_payloads"


def _pinned(root) -> str:
    m = re.search(r"^IPXE_COMMIT=(\S+)", (root / _ROLE / "files/ipxe/ipxe.conf").read_text(), re.M)
    return m.group(1)[:9] if m else "?"


def _builder(root) -> str:
    m = re.search(r"^apply_pxe_payloads_ipxe_builder:\s*(\S+)",
                  (root / _ROLE / "defaults/main.yml").read_text(), re.M)
    return m.group(1) if m else "?"


def run(argv: list, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog="reaper ipxe")
    p.add_argument("--cached", action="store_true", help="install the controller's last build, no rebuild")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--branch")
    a = p.parse_args(rest)
    comp = load_components(ctx.root, load_sites(ctx.root))["pxe_payloads"]
    comp = dataclasses.replace(comp, name="ipxe", tags=("tftp",))
    head = clone_guard(ctx.clone or ctx.root, a.branch)
    views = gather(sites, ctx)
    how = "install the cached build" if a.cached else \
        f"build iPXE {_pinned(ctx.root)} on {_builder(ctx.root)}, then install"
    for v in views:
        bangs = " ".join(b.name + (" (dormant)" if b.dormant else "") for b in v.hosts.bangs)
        print(f"ipxe {v.site.name}: {bangs}   commit {head[:7]} ({a.branch or 'main'})\n  {how}\n"
              f"  {comp.playbook} --tags tftp")
    if not a.yes:
        print("(plan only: add --yes to build and install)")
        return 0
    extra = {"apply_pxe_payloads_ipxe_build": not a.cached, "apply_pxe_payloads_ipxe_install": True}
    rc = 0
    for v in views:
        if _guarded_plays(v, [comp], ctx, extra):
            rc = 1
            continue
        live = [b.name for b in v.hosts.bangs if not b.dormant]
        rec = make_record("ipxe", head, None, ctx.user, ctx.hostname, datetime.now(timezone.utc))
        for bang, r in write_records(v.hosts, live, [rec], ctx.runner).items():
            if r.rc:
                print(f"{v.site.name}/{bang}: warn deploy record not written: {r.err.strip()}")
    return rc
