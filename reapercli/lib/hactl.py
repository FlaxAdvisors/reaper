"""reaper ha quiesce | arm | keepalived-roll | reseed: the runbook's HA procedures
(docs/runbooks/postgres-ha-and-db-updates.md) as guarded, prompt-free commands.

keepalived preempts (no nopreempt): whichever bang has the higher priority takes the
VIP as soon as its keepalived runs, and notify_master pg_promote()s it. So:
- arm never starts the declared master while the standby holds the VIP (a failback);
- keepalived-roll never re-arms the standby while the master lacks the VIP;
- reseed never targets the bang that serves."""
import signal

from reapercli.lib.ansible import run_playbook
from reapercli.lib.guards import Refused
from reapercli.lib.hapath import (_MAPPED, _interrupt, _keepalived, _set, ha_check,
                                  live_standby, standby_dormant, vip_holder)
from reapercli.lib.inventory import ssh_target
from reapercli.lib.remote import fanout

FAILBACK = "docs/runbooks/postgres-failback.md"
_SHIELD = (signal.SIGINT, *_MAPPED)


def refuse_dormant(site, sh) -> None:
    if standby_dormant(sh):
        raise Refused(f"{site.name}: standby {sh.standby[0]} is dormant: no HA pair to act on")


def keepalived_active(sh, bang: str, runner) -> bool:
    b = next(x for x in sh.bangs if x.name == bang)
    r = fanout({bang: ssh_target(b, sh.on_site)}, "systemctl is-active keepalived", None, 15, runner)[bang]
    return r.out.strip() == "active"


def quiesce(site, sh, ctx, yes: bool, out=print) -> int:
    holder = vip_holder(sh, ctx.runner)
    standby = live_standby(sh, holder)
    out(f"quiesce {site.name}: stop keepalived on {standby} (VIP holder {holder})")
    if not yes:
        out("plan only: rerun with --yes")
        return 0
    r, target = _keepalived(sh, standby, "stop", ctx.runner)
    if r.rc != 0:
        out(f"FAIL {site.name}: stop keepalived on {standby}: {r.err.strip()}")
        return 1
    out(f"{site.name}: keepalived stopped on {standby}; no failover until: reaper ha arm {site.name} --yes")
    return 0


def arm(site, sh, ctx, yes: bool, out=print) -> int:
    holder = vip_holder(sh, ctx.runner)
    live_standby(sh, holder)          # refuses unless exactly one bang holds the VIP
    targets = [b.name for b in sh.bangs
               if b.name != holder and not b.dormant and not keepalived_active(sh, b.name, ctx.runner)]
    if not targets:
        out(f"arm {site.name}: nothing to arm (keepalived active on every live bang)")
        return 0
    if sh.primary in targets:
        raise Refused(f"{site.name}: {sh.primary} is the declared master and {holder} holds the VIP: "
                      f"starting its keepalived is a failback (it preempts); follow {FAILBACK}")
    for t in targets:
        out(f"arm {site.name}: start keepalived on {t} (VIP holder {holder}), then ha check")
    if not yes:
        out("plan only: rerun with --yes")
        return 0
    rc = 0
    for t in targets:
        r, target = _keepalived(sh, t, "start", ctx.runner)
        if r.rc != 0:
            rc = 1
            out(f"FAIL {site.name}: start keepalived on {t}: run: ssh {target} sudo systemctl start keepalived")
    if ha_check(site, ctx, sh) != 0:
        rc = 1
        out(f"FAIL {site.name}: ha check after arm: see reaper ha status {site.name}")
    return rc


def wait_vip(sh, bang: str, ctx, timeout: float = 60, step: float = 2) -> bool:
    waited = 0.0
    while True:
        if vip_holder(sh, ctx.runner) == bang:
            return True
        if waited >= timeout:
            return False
        ctx.sleep(step)
        waited += step


def _apply_keepalived(site, ctx, bang: str) -> int:
    return run_playbook(site, ctx.root, "apply_bang_services.yml", tags=("keepalived",), limit=bang,
                        runner=ctx.play_runner)


def roll(site, sh, ctx, yes: bool, out=print) -> int:
    """The runbook's "Changing keepalived config", master first."""
    master = sh.primary
    holder = vip_holder(sh, ctx.runner)
    if holder != master:
        raise Refused(f"{site.name}: the roll is master-first and needs the VIP on the declared master "
                      f"{master} (holder: {holder or 'none'}); see reaper ha status {site.name}")
    standby = live_standby(sh, holder)
    steps = [f"1. quiesce: stop keepalived on {standby}",
             f"2. apply_bang_services.yml --tags keepalived --limit {master}",
             f"3. wait for {master} to hold the VIP {sh.vip}",
             f"4. apply_bang_services.yml --tags keepalived --limit {standby} (starts keepalived there)",
             "5. ha check"]
    out(f"keepalived-roll {site.name}:")
    for s in steps:
        out(f"  {s}")
    if not yes:
        out("plan only: rerun with --yes")
        return 0
    if ha_check(site, ctx, sh) != 0:
        raise Refused(f"{site.name}: ha check unhealthy before the roll; see reaper ha status {site.name}")
    rc = 0
    old = _set({s: _interrupt for s in _MAPPED})
    try:
        r, _ = _keepalived(sh, standby, "stop", ctx.runner)
        if r.rc != 0:
            raise Refused(f"{site.name}: could not stop keepalived on {standby}: {r.err.strip()}")
        if _apply_keepalived(site, ctx, master) != 0:
            rc = 1
            out(f"FAIL {site.name}: keepalived apply on {master}")
        elif not wait_vip(sh, master, ctx):
            rc = 1
            out(f"FAIL {site.name}: {master} did not take the VIP within 60 s; the standby's keepalived "
                f"config was NOT applied: once the VIP is back, rerun reaper ha keepalived-roll {site.name} --yes")
        elif _apply_keepalived(site, ctx, standby) != 0:
            rc = 1
            out(f"FAIL {site.name}: keepalived apply on {standby}")
    finally:
        shield = _set({s: signal.SIG_IGN for s in _SHIELD})
        try:
            if vip_holder(sh, ctx.runner) == master:
                r, target = _keepalived(sh, standby, "start", ctx.runner)
                armed = r.rc == 0
                if not armed:
                    out(f"FAIL {site.name}: start keepalived on {standby}: run: ssh {target} sudo systemctl start keepalived")
            else:
                armed = False
                out(f"FAIL {site.name}: master {master} does not hold the VIP; standby {standby} left quiesced (no failover).")
                out(f"Fix the master, then: reaper ha arm {site.name} --yes")
        finally:
            _set(shield)
            _set(old)
    if not armed:
        return 1
    if ha_check(site, ctx, sh) != 0:
        out(f"FAIL {site.name}: ha check after the roll: see reaper ha status {site.name}")
        return 1
    return rc


def reseed(site, sh, ctx, confirm, yes: bool, out=print) -> int:
    """The runbook's "Re-seeding": WIPE the standby's postgres, pg_basebackup from the
    primary. Only ever the declared standby, and only while the declared primary serves."""
    if not sh.standby:
        raise Refused(f"{site.name}: no declared standby: nothing to reseed")
    standby, primary = sh.standby[0], sh.primary
    if confirm is None:
        raise Refused(f"{site.name}: reseed is destructive: add --confirm {standby}")
    if confirm != standby:
        raise Refused(f"{site.name}: --confirm {confirm} is not the declared standby {standby} "
                      f"(primary {primary}); reseed only ever wipes the standby")
    holder = vip_holder(sh, ctx.runner)
    if holder != primary:
        raise Refused(f"{site.name}: the VIP is on {holder or 'no single bang'}, not the declared primary "
                      f"{primary}: reseeding {standby} could wipe the serving database; follow {FAILBACK}")
    out(f"reseed {site.name}: WIPE postgres on {standby}, pg_basebackup from {primary}; then ha check")
    if not yes:
        out("plan only: rerun with --yes")
        return 0
    if run_playbook(site, ctx.root, "pg_standby_bootstrap.yml",
                    extra={"target": standby, "pg_bootstrap_force": "true"}, runner=ctx.play_runner) != 0:
        out(f"FAIL {site.name}: pg_standby_bootstrap on {standby}; see the play output above")
        return 1
    if ha_check(site, ctx, sh) != 0:
        out(f"FAIL {site.name}: ha check after reseed: see reaper ha status {site.name}")
        return 1
    return 0
