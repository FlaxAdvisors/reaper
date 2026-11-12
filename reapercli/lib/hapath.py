"""The HA path (spec §8.3) around anything that can blip a chk_services-watched
service on the VIP holder: ha check -> stop the LIVE standby's keepalived -> body
-> ALWAYS start it again -> ha check. Without the quiesce, a healthz blip hands the
VIP to the standby and notify_master pg_promote()s it: replication detaches.

Phase 3's `ha quiesce|arm` wrap quiesce()/arm() here."""
import signal
from contextlib import contextmanager
from typing import Optional

from reapercli.lib.ansible import run_playbook
from reapercli.lib.guards import Refused
from reapercli.lib.inventory import site_hosts, ssh_target
from reapercli.lib.out import Row, render
from reapercli.lib.remote import fanout


def vip_holder(sh, runner) -> Optional[str]:
    live = {b.name: ssh_target(b, sh.on_site) for b in sh.bangs if not b.dormant}
    res = fanout(live, "ip -o -4 addr", None, 10, runner)
    held = [n for n, r in res.items() if f"inet {sh.vip}/" in r.out]
    return held[0] if len(held) == 1 else None


def standby_dormant(sh) -> bool:
    return bool(sh.standby) and any(b.dormant for b in sh.bangs if b.name == sh.standby[0])


def ha_check(site, ctx, sh=None) -> int:
    sh = sh or site_hosts(site, ctx.root, ctx.hostname)
    if standby_dormant(sh):
        print(render([Row("ha", "check", "warn", f"standby dormant, no HA ({sh.standby[0]})", site=site.name)]))
        return 0
    return run_playbook(site, ctx.root, "pg_ha_check.yml", extra={"ha_check_mode": "assert"},
                        runner=ctx.play_runner)


def live_standby(sh, holder: Optional[str]) -> str:
    if holder is None:
        raise Refused(f"{sh.site}: no single VIP holder; run: reaper ha status {sh.site}")
    others = [b for b in sh.bangs if b.name != holder and not b.dormant]
    if not others:
        raise Refused(f"{sh.site}: no live standby besides the VIP holder {holder}")
    return others[0].name


def _keepalived(sh, bang: str, action: str, runner):
    b = next(x for x in sh.bangs if x.name == bang)
    target = ssh_target(b, sh.on_site)
    r = fanout({bang: target}, f"sudo -n systemctl {action} keepalived", None, 30, runner)[bang]
    return r, target


class Guard:
    ok = True


def _interrupt(signum, frame):
    raise KeyboardInterrupt(signal.Signals(signum).name)


_MAPPED = (signal.SIGTERM, signal.SIGHUP)   # a dropped ssh session sends SIGHUP


def _set(handlers: dict) -> dict:
    """Install {sig: handler}; return the previous ones. Off the main thread: a no-op."""
    old = {}
    try:
        for sig, h in handlers.items():
            old[sig] = signal.signal(sig, h)
    except ValueError:
        pass
    return old


@contextmanager
def ha_guard(site, sh, holder, ctx, out=print):
    g = Guard()
    if standby_dormant(sh):
        out(f"{site.name}: warn: standby dormant, no HA guard ({sh.standby[0]})")
        yield g
        return
    standby = live_standby(sh, holder)
    if ha_check(site, ctx, sh) != 0:
        raise Refused(f"{site.name}: ha check unhealthy before deploy; see reaper ha status {site.name}")
    old = _set({s: _interrupt for s in _MAPPED})
    try:
        # from here on, the re-arm runs whatever happens -- even mid-stop
        r, target = _keepalived(sh, standby, "stop", ctx.runner)
        if r.rc != 0:
            raise Refused(f"{site.name}: could not stop keepalived on {standby}: {r.err.strip()}")
        out(f"{site.name}: keepalived stopped on standby {standby} (VIP holder {holder})")
        yield g
    finally:
        # shield the re-arm: a second Ctrl-C / SIGTERM / SIGHUP must not cut it short
        shield = _set({s: signal.SIG_IGN for s in (signal.SIGINT, *_MAPPED)})
        try:
            r, target = _keepalived(sh, standby, "start", ctx.runner)
            if r.rc != 0:
                g.ok = False
                out(f"{site.name}: FAIL re-arm {standby}: run: ssh {target} sudo systemctl start keepalived")
            else:
                out(f"{site.name}: keepalived re-armed on {standby}")
        finally:
            _set(shield)
            _set(old)
        if ha_check(site, ctx, sh) != 0:
            g.ok = False
            out(f"{site.name}: FAIL ha check after: see reaper ha status {site.name}")
