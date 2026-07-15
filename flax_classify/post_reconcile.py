"""Post-lane reservation lifecycle: pure reconcile decision + DB apply.

`plan_post_reconcile` decides, from plain data, which source='post' reservations
to evict (blade replaced, or port link-down past the window) and which debounce
timers to write into user_context.post. `reconcile_post_reservations` is the thin
DB apply layer. See
docs/superpowers/specs/2026-07-02-post-reservation-lifecycle-design.md.
"""
import collections
import datetime
import logging

from .formula import _normalise_rabbit_port
from .kea_hosts import read_post_reservations, stamp_post_timers
from .desired_reservations import delete_desired, upsert_desired

log = logging.getLogger(__name__)

ReconcilePlan = collections.namedtuple("ReconcilePlan", ["deletes", "timer_writes"])


def _norm(mac):
    return mac.strip().lower()


def _key(port):
    """Reservation token or arista port -> (p, s), or None if unparseable."""
    try:
        return _normalise_rabbit_port(port)
    except (ValueError, AttributeError):
        return None


def _elapsed(iso_since, now):
    try:
        return (now - datetime.datetime.fromisoformat(iso_since)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def plan_post_reconcile(reservations, switch_facts, now, cfg):
    """Compute {deletes, timer_writes} for source='post' reservations.

    See module docstring + spec. Pure: no I/O, deterministic given `now`.
    """
    # Unreachable in practice: __main__._post_reconcile_cfg always sets this
    # key (registry value or its own -1 fail-safe). Defense-in-depth only --
    # every layer degrades to inert (disabled), never to the old 900s eager
    # eviction default.
    linkdown_secs = cfg.get("linkdown_evict_secs", -1.0)
    replace_secs = cfg.get("replace_debounce_secs", 60.0)
    flash_macs = {_norm(m) for m in cfg.get("flash_macs") or ()}
    rule3_on = linkdown_secs is not None and linkdown_secs > 0
    rule2_on = replace_secs is not None and replace_secs >= 0

    # Per (switch, (p,s)): the port fact, and the set of reserved macs there.
    port_index = {}
    for sw, body in (switch_facts or {}).items():
        for aport, info in (body.get("ports") or {}).items():
            k = _key(aport)
            if k is not None:
                port_index[(sw, k)] = info
    reserved_by_port = collections.defaultdict(set)
    for r in reservations:
        k = _key(r.get("port"))
        if k is not None:
            reserved_by_port[(r["switch"], k)].add(_norm(r["mac"]))

    deletes = []
    timer_writes = {}
    for r in reservations:
        mac = _norm(r["mac"])
        if r.get("operator_note") or mac in flash_macs:
            continue
        k = _key(r.get("port"))
        info = port_index.get((r["switch"], k)) if k is not None else None
        if info is None:
            # No port fact: switch unreachable (empty ports row) or the port is
            # absent from switch_facts. Deliberately conservative -- leave the
            # reservation and its timers untouched rather than risk evicting on
            # missing data. (A reachable switch normally reports an idle port as
            # 'nolink', so Rule 3 still governs genuine vacancy.)
            continue

        link = info.get("link")
        port_macs = {_norm(m) for m in (info.get("macs") or [])}
        post = dict(r.get("post") or {})
        new_post = dict(post)
        delete = False

        # Rule 3: link-down debounce.
        if link == "link":
            new_post.pop("link_down_since", None)
        elif link == "nolink":
            since = new_post.get("link_down_since") or now.isoformat()
            new_post["link_down_since"] = since
            if rule3_on and _elapsed(since, now) >= linkdown_secs:
                delete = True
        # link == "unknown" (or anything else): leave link_down_since untouched.

        # Rule 2: replaced-blade debounce. Only when link is definitively up and
        # a foreign (non-reserved) mac occupies the port.
        if not delete:
            if link == "link":
                if mac not in port_macs:
                    foreign = port_macs - reserved_by_port.get((r["switch"], k), set())
                    if foreign:
                        since = new_post.get("mac_absent_since") or now.isoformat()
                        new_post["mac_absent_since"] = since
                        if rule2_on and _elapsed(since, now) >= replace_secs:
                            delete = True
                    else:
                        # No foreign occupant (e.g. BMC dark mid-flash): flash-safety,
                        # do not treat as a replace.
                        new_post.pop("mac_absent_since", None)
                else:
                    # mac present: blade is back.
                    new_post.pop("mac_absent_since", None)
            elif link == "nolink":
                # Absence is expected while down; Rule 3 governs eviction here.
                new_post.pop("mac_absent_since", None)
            # link == "unknown" (or anything else): leave mac_absent_since
            # untouched, mirroring Rule 3's explicit no-op above.

        if delete:
            deletes.append(mac)
        elif new_post != post:
            timer_writes[mac] = new_post

    return ReconcilePlan(deletes, timer_writes)


def reconcile_post_reservations(pool, *, facts, now, cfg,
                                derived_macs: frozenset = frozenset()):
    """Read source='post' reservations, plan the reconcile, apply it: stamp
    debounce timers and mirror the eviction/keep decisions into
    desired_reservations. Returns {"deleted", "timers"}. Best-effort per
    call -- the post lane wraps it.

    Post-3b demolition: the LEGACY delete_post_hosts call this function used
    to make (evicting the kea.hosts row directly) is gone -- the
    materializer's apply layer is now the sole post kea deleter, reading
    desired_reservations' absence of an evicted mac and deleting the
    corresponding kea row on its own pass. `deleted` below now counts the
    plan's eviction decisions themselves (len(plan.deletes)), not a kea
    rowcount.

    stamp_post_timers is the documented metadata exception: lifecycle
    timers live only in user_context.post, not a kea.hosts row identity, so
    writing them is independent of who deletes the kea row and must always
    run regardless of anything else in this function.

    Keep-set desired emission (phase 3a, Task 3): every reservation the plan
    does NOT evict is upserted into desired_reservations (owner_role="post").
    Unlike triage's "what I was told to reserve" derived-target writes, the
    post lane's lifecycle is retention-based -- a reservation's continued
    existence is a decision made HERE, by this engine, from current switch
    state + the debounce rules in plan_post_reconcile. "What I would keep"
    legitimately derives from that decision, so the materializer's future
    desired-vs-actual diff must see the post-decided keep-set, not just the
    raw reservation rows. The materializer (not this module) still owns
    reconciling desired_reservations into materializer_plan.

    derived_macs (final-review fix): macs (normalised lowercase) that
    `run_post_reservations` already upserted a FRESH desired row for earlier
    in this same `run_post_lane` pass. The keep-set loop below skips them --
    it covers exactly the complement, i.e. quiet ports the reserve pass no
    longer derives a target for. Without this, the keep-set unconditionally
    echoed the ACTUAL kea row's (possibly stale) values for every retained
    reservation, including ones the reserve pass just re-derived with fresh
    values (new order prefix, vid change, etc); since the keep-set runs
    AFTER the reserve pass in the same cycle, that echo permanently
    clobbered the freshly-derived desired row back to the old actual values.
    Under post-enforce (legacy kea write gated) that echo was a deadlock:
    the derived row could never converge because this pass immediately
    overwrote it every cycle. Default empty frozenset preserves prior
    behavior for any caller that doesn't thread it through.

    kind fallback: read_post_reservations' kind column is a bare
    classify->>'kind' jsonb extraction (kea_hosts._READ_POST_SQL) with no
    COALESCE, so it is NULL whenever a post row's classify context predates
    the 'kind' key (legacy rows) or otherwise omits it -- "bmc" is the
    correct default since post-lane BMC reservations are the overwhelming
    common case and the historical implicit kind.
    """
    reservations = read_post_reservations(pool)
    plan = plan_post_reconcile(reservations, facts, now, cfg)
    if plan.timer_writes:
        stamp_post_timers(pool, plan.timer_writes)
    deleted = len(plan.deletes)
    if plan.deletes:
        # desired_reservations write: evict these macs from owner_role="post"
        # so the materializer notices their disappearance and deletes the
        # corresponding kea row on its own next pass. Guarded so a write
        # failure (incl UndefinedTable pre-migration) never breaks the
        # reconcile.
        try:
            delete_desired(pool, owner_role="post", macs=plan.deletes)
        except Exception:
            log.exception(
                "desired write (post delete) failed for macs=%s", plan.deletes)

    # Keep-set desired emission (Task 3): every reservation the plan retains
    # (i.e. NOT in plan.deletes) AND that the reserve pass did NOT already
    # derive fresh this cycle (i.e. NOT in derived_macs) is upserted into
    # desired_reservations. Per-row try/except (final-review fix; was one
    # try/except around the whole pass) -- a keep-set failure for one mac
    # (e.g. missing table pre-migration, or a mid-pass DB error for that row)
    # must never break the reconcile NOR block the remaining macs' keep-set
    # upserts this cycle; the timer stamp + delete-mirror above have already
    # applied by this point regardless.
    deleted_macs = set(plan.deletes)
    for r in reservations:
        mac = _norm(r["mac"])
        if mac in deleted_macs or mac in derived_macs:
            continue
        try:
            upsert_desired(
                pool, owner_role="post", mac=mac, kind=r["kind"] or "bmc",
                hostname=r["hostname"], ipv4=r["ipv4"], ipv6=r["ipv6"],
                vid=r["vid"], switch=r["switch"], port=r["port"],
            )
        except Exception:
            log.exception(
                "desired write (post keep-set) failed for mac=%s", mac)

    return {"deleted": deleted, "timers": len(plan.timer_writes)}
