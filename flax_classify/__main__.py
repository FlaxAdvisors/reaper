# flax_classify/__main__.py
"""flax-classify entrypoint.

  Cycle on a timer (--cycle-secs, default 30).
  Cycle on every debounced LISTEN ping (default 1.5s window).
  Healthz on --healthz-port (default 10991).
"""
import argparse
import logging
import os
import sys
import threading
import time

from . import role_registry
from .cycle import run_one_cycle
from .db import (build_pool, db_now, read_observe_rows, read_post_order,
                 read_switch_facts)
from .desired_port import write_ack
from .kea_hosts import read_kea_actuals
from .dns_hosts import write_hosts_file
from .materializer import parse_enforce_config, run_cycle
from .post_reconcile import reconcile_post_reservations
from .post_reserve import (observed_by_port, read_post_racks, read_post_slots,
                           run_post_reservations)
from .healthz import HealthState, serve as serve_healthz
from .listen import Debouncer, listen_loop
from .vlan_policy import (load_fp_to_vid, load_phase_geometry, load_no_steer,
                          load_bmc_only_families, fp_to_vid_from_roles)


log = logging.getLogger("flax-classify")


def _ack_action(summary: dict) -> str:
    """applied if the cycle wrote any kea reservation OR desired_port row this
    cycle, else noop."""
    if summary.get("written") or summary.get("written_desired"):
        return "applied"
    return "noop"


def _ack_cycle(pool, generation, summary: dict) -> None:
    """Success-path consumer_acks write. Wrapped in its own try/except so a
    ledger write failure can never crash the cycle."""
    try:
        write_ack(pool, "flax-classify", "observe_state", generation,
                  _ack_action(summary))
    except Exception:
        log.exception("write_ack (success) failed; continuing")


def _ack_failed(pool, generation, exc) -> None:
    """Except-path consumer_acks write. detail truncated to 200 chars; these
    services' exceptions carry no credentials."""
    write_ack(pool, "flax-classify", "observe_state", generation, "failed",
              detail=str(exc)[:200])


def _build_conninfo() -> str:
    """Resolve from env vars (PGHOST, PGUSER, etc.) -- mirrors
    flax_observe.db._dsn_from_env. psycopg DSN keys are host=, port=,
    user=, password=, dbname= (NOT pghost=). Lowercasing the env names
    would produce bogus DSN keys -- map explicitly.
    """
    mapping = (
        ("PGHOST", "host"), ("PGPORT", "port"),
        ("PGUSER", "user"), ("PGPASSWORD", "password"),
        ("PGDATABASE", "dbname"),
    )
    parts = []
    for env_k, dsn_k in mapping:
        v = os.environ.get(env_k)
        if not v:
            raise RuntimeError(f"required env var {env_k} is not set")
        parts.append(f"{dsn_k}={v}")
    parts.append("application_name=flax-classify")
    return " ".join(parts)


def _post_reconcile_cfg(cycle_secs, role_defs):
    """Post reconcile knobs, registry-first (Task 4 cutover: the
    POST_LINKDOWN_EVICT_SECS/POST_REPLACE_DEBOUNCE_CYCLES env-var reads are
    RETIRED -- ansible's env lines are removed in Task 5). Sourced from
    role_defs["post"]["policy"]["lifecycle"] (linkdown_evict_secs /
    replace_debounce_cycles), each key falling back independently to the
    code default when the registry is empty, has no "post" role, or doesn't
    carry that key -- deploy-order safety for a registry that predates this
    policy or failed to publish (callers pass {} in that case, same gate as
    _effective_enforced_roles).

    linkdown_evict_secs code-default is -1 (Rule 3 DISABLED), NOT 900: a
    registry-degraded startup (empty/missing roles.d) must never silently
    ARM Rule 3 against link_down_since timers that may be months old --
    on rabbit-edam most post ports are link-down (racked-but-off,
    provision-before-boot), so waking up with a live 900s threshold would
    depopulate the whole racked inventory in one "thundering herd" cycle.
    The fail-safe fallback mirrors the base role default (see git history:
    apply_flax_classify_post_linkdown_evict_secs was -1 before the vars were
    retired in Task 5) -- degrade to inert, not to the ordinary-operation
    value. replace_debounce_cycles keeps its 2-cycle code default; Rule 2
    (replace-on-new-blade) has no equivalent stale-timer hazard.

    Rule 3 (link-down depopulation) disabled if secs <= 0; Rule 2
    (replace-on-new-blade) disabled if cycles < 0 (0 cycles = immediate on a
    foreign occupant)."""
    lifecycle = ((role_defs or {}).get("post") or {}).get("policy", {}).get(
        "lifecycle", {}) or {}
    evict = float(lifecycle.get("linkdown_evict_secs", -1))
    cycles = int(lifecycle.get("replace_debounce_cycles", 2))
    replace = cycles * cycle_secs if cycles >= 0 else -1.0
    return {"linkdown_evict_secs": evict,
            "replace_debounce_secs": replace,
            "flash_macs": set()}


def _effective_enforced_roles(enforced_roles, role_defs) -> frozenset:
    """Intersect the parsed MATERIALIZER_ENFORCE_ROLES with the ACTIVE role
    registry (arming safety). run_cycle (the materializer) only plans/applies
    for owners the registry holds -- so arming a role the registry does NOT
    hold would enforce a role run_cycle can never actually apply for: a
    silent no-op arm.

    Post-3b demolition: this intersection no longer protects a legacy
    write-stop (the engines have no legacy kea writers left to gate) --
    removing a role from MATERIALIZER_ENFORCE_ROLES now simply freezes that
    role's reservations at the materializer layer (see the run_cycle arming
    docstring in main() below for the full ops-semantics note). This
    function still exists purely to keep an operator from arming a role the
    registry doesn't know about.

    Any role stripped here is loudly log.error'd. `role_defs` must be the
    registry that actually PUBLISHED ({} when the registry fell back /
    resolve is None -- an inactive registry enforces nothing).
    """
    enforced = frozenset(enforced_roles)
    effective = enforced & set(role_defs)
    stripped = enforced - effective
    if stripped:
        log.error("MATERIALIZER_ENFORCE_ROLES contains roles not in the "
                  "active registry: %s - NOT enforcing them (run_cycle has "
                  "no owner to apply/plan for)", sorted(stripped))
    return effective


def _post_dns_records(actuals):
    """Project source='post' kea actuals into dns_hosts records.

    read_kea_actuals rows carry {source, hostname, ipv4, ipv6, ...}; keep only
    post-owned rows that have a hostname (a reservation with no hostname can't
    be an A/AAAA record), as {hostname, ipv4, ipv6}. ipv4/ipv6 may be None --
    render_hosts skips falsy addresses.
    """
    return [{"hostname": a["hostname"], "ipv4": a.get("ipv4"),
             "ipv6": a.get("ipv6")}
            for a in actuals
            if a.get("source") == "post" and a.get("hostname")]


def run_post_lane(pool, geometry_path, cycle_secs, role_defs=None,
                  post_dns_path="/etc/dnsmasq.hosts/flax-post-devices"):
    """Continuous post reservation lane, isolated: a post failure must never break
    the triage cycle or its ack. Populates from switch_facts, then reconciles
    (evict replaced blades / depopulate link-down ports).

    Post-3b demolition: run_post_reservations and reconcile_post_reservations
    write desired_reservations only now (no more legacy kea writers to gate),
    so this wrapper no longer threads an enforced/enforced_roles config into
    either call.

    role_defs (Task 4): threaded into _post_reconcile_cfg for the registry-
    first lifecycle knobs; defaults to {} (code-default lifecycle) so direct
    callers/tests that predate this param still work unchanged.

    derived_macs (final-review fix): run_post_reservations' summary carries
    the macs it upserted a fresh desired row for this cycle; threaded into
    reconcile_post_reservations so its keep-set pass doesn't clobber those
    fresh rows with the (possibly stale) actual-kea-row echo. summary.get(...)
    defaults to an empty frozenset for the order_no-falsy no-op summary shape
    ({"written": 0, "purged": 0}), which carries no derived_macs key.
    """
    try:
        order = read_post_order(pool)
        racks = read_post_racks(geometry_path)
        slots = read_post_slots(geometry_path)
        facts = read_switch_facts(pool)
        observed = observed_by_port(read_observe_rows(pool))
        summary = run_post_reservations(pool, order_no=order, racks=racks,
                                        facts=facts, observed=observed, slots=slots)
        if summary["written"] or summary["purged"]:
            log.info("post-reserve written=%d purged=%d",
                     summary["written"], summary["purged"])
        recon = reconcile_post_reservations(
            pool, facts=facts, now=db_now(pool),
            cfg=_post_reconcile_cfg(cycle_secs, role_defs or {}),
            derived_macs=summary.get("derived_macs", frozenset()))
        if recon["deleted"]:
            log.info("post-reconcile deleted=%d timers=%d",
                     recon["deleted"], recon["timers"])
    except Exception:
        log.exception("post reservation lane failed")

    # Post DNS: publish A/AAAA for source='post' reservations to the post
    # hostsdir file (dnsmasq inotify-reloads it). Isolated in its own
    # try/except -- a DNS write failure must never break the reservation lane,
    # and a reservation failure above must not skip DNS (kea still holds the
    # last-good reservations).
    try:
        write_hosts_file(post_dns_path, _post_dns_records(read_kea_actuals(pool)))
    except Exception:
        log.exception("post DNS write failed")


def main(argv=None):
    p = argparse.ArgumentParser(prog="flax-classify")
    p.add_argument("--cycle-secs", type=float, default=30.0,
                   help="Periodic cycle interval (LISTEN-debounced calls "
                        "happen independently)")
    p.add_argument("--debounce-secs", type=float, default=1.5,
                   help="Coalesce LISTEN pings into one cycle per window")
    p.add_argument("--healthz-port", type=int, default=10991)
    p.add_argument("--healthz-stale-secs", type=float, default=60.0)
    p.add_argument("--vlans", default="/etc/flax/vlans.json",
                   help="Path to vlans.json (family+phase -> vid mapping)")
    p.add_argument("--geometry", default="/etc/flax/geometry.json",
                   help="Path to geometry.json (triage port tokens)")
    p.add_argument("--no-steer", dest="no_steer",
                   default="/etc/flax/no-steer-ports.json",
                   help="Path to no-steer-ports.json (excluded uplink ports)")
    p.add_argument("--bmc-only", dest="bmc_only",
                   default="/etc/flax/bmc-only-families.json",
                   help="Path to bmc-only-families.json (RJ45-LOM families "
                        "whose single MAC serves both BMC + NIC; suppresses "
                        "the phantom host reservation)")
    p.add_argument("--post-geometry", default="/etc/flax/post-geometry.json",
                   help="post rack geometry (racks->tag) for the reservation lane")
    p.add_argument("--post-dns-hosts",
                   default="/etc/dnsmasq.hosts/flax-post-devices",
                   help="dnsmasq hostsdir file for source=post DNS (A/AAAA)")
    p.add_argument("--triage-dns-hosts",
                   default="/etc/dnsmasq.hosts/flax-triage-devices",
                   help="dnsmasq hostsdir file for triage device DNS (A/AAAA)")
    p.add_argument("--roles-dir", default=role_registry.DEFAULT_ROLES_DIR,
                   help="Path to roles.d (role registry); missing/empty dir "
                        "or an invalid registry falls back to the legacy "
                        "geometry phase_for (phase-1, removed in phase 3)")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    conninfo = _build_conninfo()
    pool = build_pool(conninfo)
    health = HealthState(stale_secs=args.healthz_stale_secs)

    # Two-key arming -- MATERIALIZER_MODE=enforce plus a named role in
    # MATERIALIZER_ENFORCE_ROLES is required to enforce anything; the
    # deployed default (MODE unset/"shadow") is a global kill-switch
    # (parse_enforce_config logs its own error on an invalid mode value).
    # Post-3b demolition: enforced_roles/max_deletes_per_run are now threaded
    # ONLY into run_cycle (the materializer's apply layer) below -- see the
    # ops-semantics note at that call site for what "removing a role from
    # MATERIALIZER_ENFORCE_ROLES" now means with no legacy writers left.
    enforced_roles, max_deletes_per_run = parse_enforce_config(dict(os.environ))
    log.info("materializer enforce config: enforced_roles=%s "
             "max_deletes_per_run=%d",
             sorted(enforced_roles), max_deletes_per_run)

    # Load steering policy files once at startup; they change only on redeploy.
    # fp_to_vid is resolved further down (after the role registry section)
    # because it now prefers the registry (Task 4) over vlans.json.
    geom_tokens = load_phase_geometry(args.geometry)
    no_steer = load_no_steer(args.no_steer)
    bmc_only = load_bmc_only_families(args.bmc_only)

    # Role registry (spine migration phase 1): load -> validate -> publish ->
    # ack, in that strict order -- an invalid registry must never publish,
    # and an empty/missing one must never publish (would wipe roles/
    # role_universe via a full-replace). Both failure paths fall back to
    # resolve=None, i.e. the legacy geom_tokens/phase_for steering the feeder
    # already had (phase-1 deploy-order safety, removed in phase 3).
    resolve = None
    role_defs = {}
    try:
        role_defs = role_registry.load_role_dir(args.roles_dir)
        if role_defs:
            role_registry.validate_roles(role_defs)
            gen = role_registry.publish_roles(pool, role_defs)
            # action must satisfy consumer_acks_action_check (migration 003:
            # applied|noop|deferred|failed|skipped) -- 'publish' violated it
            # on the live DB and tripped the startup fallback (2026-07-03).
            write_ack(pool, "flax-classify", "roles", gen, "applied")
            resolve = lambda sw, p: role_registry.resolve_role(role_defs, sw, p)
            log.info("role registry: %d roles, generation %d",
                     len(role_defs), gen)
        else:
            log.warning("roles.d missing/empty at %s - legacy geometry "
                        "phase_for (phase-1 fallback, removed in phase 3)",
                        args.roles_dir)
    except role_registry.RegistryError:
        log.exception("role registry invalid - refusing to publish; "
                      "legacy phase_for")
    except Exception:
        # Any other failure in load/validate/publish/ack (e.g. a DB error
        # because migration 029 hasn't been applied yet) must not crash
        # classify -- fall back to the legacy geometry phase_for exactly
        # like the RegistryError path above.
        log.exception("role registry startup publish failed - legacy "
                      "phase_for fallback")

    # Registry-gated view (Task 4): policy derivation (vid steering, post
    # lifecycle knobs) trusts role_defs ONLY when the registry actually
    # validated + published this cycle (resolve is not None) -- the same
    # deploy-order-safety gate _effective_enforced_roles already applies to
    # MATERIALIZER_ENFORCE_ROLES below. A populated-but-unvalidated role_defs
    # (e.g. validate_roles raised after load_role_dir succeeded) must not
    # leak its data into policy just because the dict happens to be
    # non-empty -- resolve is None means role resolution itself has already
    # fallen back to the legacy geometry phase_for, so vid/lifecycle policy
    # falls back in lockstep rather than mixing registry policy with legacy
    # role resolution.
    active_role_defs = role_defs if resolve is not None else {}

    # Vid steering policy: registry-first (Task 4), vlans.json fallback.
    registry_fp_to_vid = fp_to_vid_from_roles(active_role_defs)
    if registry_fp_to_vid is not None:
        fp_to_vid = registry_fp_to_vid
        log.info("vid policy: registry")
    else:
        fp_to_vid = load_fp_to_vid(args.vlans)
        log.info("vid policy: vlans.json fallback")
    log.info("policy loaded: %d vlan entries, %d geom tokens, %d no-steer "
             "ports, %d bmc-only families",
             len(fp_to_vid), len(geom_tokens), len(no_steer), len(bmc_only))

    # Arming safety: only roles the ACTIVE registry holds may be enforced
    # (resolve is None means no registry published at all, so nothing may be
    # enforced regardless of role_defs' load state). The intersected set --
    # not the raw parse -- is what gets threaded into run_cycle below.
    enforced_roles = _effective_enforced_roles(enforced_roles, active_role_defs)

    # Monotonic per-process cycle counter used as the consumer_acks generation.
    # read_observe_rows does not expose observe_state.generation and the plan
    # forbids adding a DB query just for it; generation is informational on the
    # dashboard (it gates on freshness + action), so the counter is the
    # documented fallback. GREATEST in write_ack keeps the row monotonic.
    gen_counter = [0]

    def _do_cycle():
        gen_counter[0] += 1
        try:
            summary = run_one_cycle(pool,
                                    fp_to_vid=fp_to_vid,
                                    geom_tokens=geom_tokens,
                                    no_steer=no_steer,
                                    bmc_only=bmc_only,
                                    resolve=resolve,
                                    dns_hosts_path=args.triage_dns_hosts)
            log.info("cycle written=%d deleted=%d skipped=%d written_desired=%d",
                     summary["written"], summary["deleted"], summary["skipped"],
                     summary.get("written_desired", 0))
            health.record_cycle_done(**summary)
            _ack_cycle(pool, gen_counter[0], summary)
        except Exception as e:
            log.exception("cycle failed")
            _ack_failed(pool, gen_counter[0], e)
        # Shadow materializer (Task 5): only when the role registry is active
        # -- resolve is None means legacy phase_for fallback, i.e. no owner
        # scoping exists yet, so the shadow machinery must also stand down.
        # Isolated exactly like run_post_lane: a shadow failure must never
        # break the triage cycle or its ack.
        if resolve is not None:
            # Ops semantics (post-3b): run_cycle (the materializer) is now
            # the ONLY place enforced_roles/MATERIALIZER_ENFORCE_ROLES has
            # any effect -- cycle.py, post_reserve.py and post_reconcile.py
            # have no legacy kea writers left to gate; they always write
            # desired_reservations unconditionally. Removing a role from
            # MATERIALIZER_ENFORCE_ROLES therefore does NOT roll back to a
            # pre-3b legacy writer for that role -- there is none anymore.
            # It freezes that role's kea.hosts reservations exactly where
            # they are: run_cycle stops planning/applying upserts, deletes,
            # and purge_handoffs for it, while desired_reservations keeps
            # being written and drifting away underneath. This is an
            # intentional safe degradation (a kill switch that stops writing
            # kea, not a way to bring back a writer that no longer exists),
            # not a rollback path -- re-arming the role is what makes kea
            # catch back up to desired_reservations' current state.
            try:
                cycle_summary = run_cycle(pool, set(role_defs),
                                          enforced_roles=enforced_roles,
                                          max_deletes=max_deletes_per_run)
                by_action = cycle_summary.get("by_action", {})
                skipped = cycle_summary.get("skipped", {})
                log.info("materializer cycle: planned=%d upsert=%d delete=%d "
                         "purge_handoff=%d skipped_unowned=%d "
                         "skipped_unregistered_desired=%d "
                         "skipped_multi_actual=%d applied=%d apply_errors=%d "
                         "skipped_operator_note=%d breaker_tripped=%s",
                         cycle_summary.get("planned", 0),
                         by_action.get("upsert", 0),
                         by_action.get("delete", 0),
                         by_action.get("purge_handoff", 0),
                         skipped.get("unowned", 0),
                         skipped.get("unregistered_desired", 0),
                         skipped.get("multi_actual", 0),
                         cycle_summary.get("applied", 0),
                         cycle_summary.get("apply_errors", 0),
                         cycle_summary.get("skipped_operator_note", 0),
                         cycle_summary.get("breaker_tripped", []))
            except Exception:
                log.exception("materializer cycle failed")
        run_post_lane(pool, args.post_geometry, args.cycle_secs,
                      active_role_defs, post_dns_path=args.post_dns_hosts)

    debouncer = Debouncer(target=_do_cycle, debounce_secs=args.debounce_secs)
    debouncer.start()

    threading.Thread(target=serve_healthz, args=(health, args.healthz_port),
                     daemon=True).start()
    threading.Thread(target=_listen_with_reconnect,
                     args=(conninfo, debouncer), daemon=True).start()

    # Periodic cycle (LISTEN-independent fallback)
    while True:
        _do_cycle()
        time.sleep(args.cycle_secs)


def _listen_with_reconnect(conninfo, debouncer):
    """Reconnect-forever wrapper around listen_loop; survives DB restarts."""
    while True:
        try:
            listen_loop(conninfo, debouncer)
        except Exception:
            log.exception("LISTEN loop crashed; reconnecting in 5s")
            time.sleep(5.0)


if __name__ == "__main__":
    main(sys.argv[1:])
