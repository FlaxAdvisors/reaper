"""reaper migrate [site|all] [--tag T] [--yes] (spec §6): ha check -> apply_migrations.yml
-> ha check. Schema only, on the primary by evidence (the playbook ends on a standby);
the image tag defaults to the inventory's apply_flax_post_image_tag."""
import argparse

from reapercli.lib.ansible import run_playbook
from reapercli.lib.guards import Refused
from reapercli.lib.hapath import ha_check
from reapercli.lib.sites import resolve_scope


def run(argv: list, ctx) -> int:
    sites, rest = resolve_scope(argv, ctx.root, ctx.home)
    p = argparse.ArgumentParser(prog="reaper migrate")
    p.add_argument("--tag")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(rest)
    extra = {"migration_image_tag": a.tag} if a.tag else {}
    tag = f" -e migration_image_tag={a.tag}" if a.tag else ""
    for s in sites:
        print(f"migrate {s.name}: ha check -> apply_migrations.yml{tag} -> ha check")
    if not a.yes:
        print("(plan only: add --yes to migrate)")
        return 0
    rc = 0
    for s in sites:
        if ha_check(s, ctx) != 0:
            raise Refused(f"{s.name}: ha check unhealthy before migrate; see reaper ha status {s.name}")
        if run_playbook(s, ctx.root, "apply_migrations.yml", extra=extra, runner=ctx.play_runner):
            print(f"{s.name}: FAIL apply_migrations.yml")
            rc = 1
        if ha_check(s, ctx) != 0:
            print(f"{s.name}: FAIL ha check after migrate")
            rc = 1
    return rc
