"""One reconcile cycle: detect mismatches -> enqueue -> drain queue -> kick.

The ladder is injectable (default flax_reconcile.kick.run_ladder) so the cycle
is unit-testable without switches/BMCs. VID for the bmc_ll rung is derived from
the device's reserved subnet at claim time (reuse db.resolve_location + the
vlan the port is on); for braintree pool-mode the bmc_ll rung is best-effort and
the flap rung is the reliable backstop.

PASS 1 is the VLAN steer (classify's desired_port vs live switch_facts): a steer
sets the access VLAN and flaps the port (sentinel-first), which re-DHCPs the
device onto its reservation -- so a steered port is removed from the PASS 2 kick
set (the steer subsumes it). PASS 2 is the lease!=reservation kick.
"""
import json
import logging

from . import db, queue, actions, mismatch, steer, sentinel, kick as kick_mod
from . import bmc_reset as bmc_reset_mod
from .claims import bmc_fw_claim_active
from .config import DEFAULTS as _CFG_DEFAULTS
from .mismatch import _norm as mismatch_norm
from .portname import to_arista, to_internal

log = logging.getLogger("flax-reconcile.cycle")


# Reasons whose flap should be preceded by a stale-lease release, so the re-DHCP
# the flap triggers lands on the reservation instead of Kea renewing the pool
# lease. This covers the auto mismatch kick AND an operator-initiated flap: an
# operator flapping a device that is on the wrong (pool) lease wants it re-homed
# just like the auto path, but used to skip the release and so never converged
# (the flap/eth0-bounce only makes the client renew the stale lease). NOT
# bmc-reset: that is a Redfish reset, not a flap, and is handled before this
# point. db.release_stale_lease() is itself self-guarding (no-op when the lease
# already matches the reservation), so this gate is about intent, not safety.
_FLAP_REASONS_NEEDING_RELEASE = frozenset({"lease!=reservation", "operator flap"})


def _release_before_flap(reason):
    """True if a stale-lease release should run before flapping for this reason."""
    return reason in _FLAP_REASONS_NEEDING_RELEASE


class Reconciler:
    def __init__(self, *, switches, config, obmc_user, obmc_pass, host_creds,
                 no_steer=None, ladder=None, vlan_parents=None,
                 redfish_creds=None, bmc_reset_fn=None, eligible_sources=None):
        self.switches = switches
        # Overlay the caller's config over DEFAULTS so the circuit-breaker knobs
        # (flap_circuit_*) always resolve even for callers/tests that pass a
        # partial config dict. The loaded /etc/flax/reconcile.json already
        # carries these (config.load_config), so this is a belt-and-suspenders
        # default, not a second source of truth.
        self.cfg = dict(_CFG_DEFAULTS)
        self.cfg.update(config or {})
        self.obmc_user = obmc_user
        self.obmc_pass = obmc_pass
        self.host_creds = host_creds
        self.no_steer = no_steer or set()
        self.ladder = ladder or kick_mod.run_ladder
        # Redfish creds (list of {bmcuser, bmcpass}) + the reset fn for the
        # operator "Reset BMC (Redfish)" request reason. bmc_reset_fn is
        # injectable for tests (mirrors the ladder seam).
        self.redfish_creds = redfish_creds or []
        self.bmc_reset_fn = bmc_reset_fn or bmc_reset_mod.reset_bmc_via_redfish
        # vid (int) -> parent iface name (str), loaded from vlans.json by the
        # entrypoint and threaded into every run_ladder call so the bmc_ll rung
        # probes the correct host interface.
        self.vlan_parents = vlan_parents or {}
        # frozenset[str] | None -- role_caps.read_reconcile_eligible_sources's
        # return value, read ONCE at startup by the entrypoint (registry
        # capability lookup, spec 2026-07-03). None (the default, and every
        # test/caller that predates this) preserves the exact pre-registry
        # `source <> 'post'` filter in db.read_reservations.
        self.eligible_sources = eligible_sources

    def run_one_cycle(self, pool) -> dict:
        # ---- PASS 1: VLAN steer (precedence; a steer subsumes the kick) ----
        sf_ports = db.read_switch_facts_ports(pool)
        steers = steer.compute_steers(db.read_desired_ports(pool), sf_ports,
                                      self.no_steer)
        steered_ports, refused = set(), 0
        for s in steers:
            if s["action"] == "steer":
                if self._do_steer(pool, s):
                    steered_ports.add((s["switch"], s["port"]))
            else:
                refused += 1
                actions.log_action(
                    pool, switch=s["switch"], port=s["port"], action="set_vlan",
                    detail={"desired_vid": s["desired_vid"],
                            "current_vid": s["current_vid"]},
                    outcome="deferred", reason=s["reason"])
                _emit_fault(pool, s)  # audit.events row for operator visibility

        # ---- PASS 2: lease!=reservation kick (skip ports we just steered) ----
        # Reclaim crash-stranded 'claimed' rows first: if the process died
        # between claim_next and complete/defer last cycle, the row is stuck
        # 'claimed' and both blocks re-enqueue of that mac (open-mac unique
        # index) and is never re-claimed (claim_next only takes 'pending') --
        # the device is stranded forever. Reset claims older than the threshold
        # back to 'pending' so they re-process this cycle.
        reclaimed = queue.reclaim_stale_claims(
            pool, older_than_secs=self.cfg["reclaim_stale_claim_secs"])
        mismatches = mismatch.compute_mismatches(
            db.read_active_leases(pool),
            db.read_reservations(pool, eligible_sources=self.eligible_sources))
        # Convergence circuit-breaker (per-MAC). A reserved device that has
        # converged onto its IP simply stops being flapped, so its last_flap_at
        # ages out -- GC the row time-based (NOT by mismatch-membership). The
        # old membership-clear wiped a per-port flap-pong MAC's cooldown the
        # instant it momentarily converged: a port with two MACs (host+bmc)
        # alternates, whichever converges this cycle got its row DELETED, so it
        # re-mismatched next cycle with no cooldown gate and the port
        # flap-ponged every ~15s forever, never accumulating to backoff. Aging
        # by backoff_secs keeps a flap-pong MAC's fresh row alive to accumulate,
        # while a genuinely-converged MAC's row ages away. For still-mismatched
        # macs the row gates re-enqueue: cooldown-on-success spacing, then
        # backoff after N flaps.
        flap_state = db.read_flap_state(pool)
        now = db.db_now(pool)
        db.clear_stale_flap_state(
            pool, older_than_secs=self.cfg["flap_circuit_backoff_secs"])
        # Ports whose host is mid-PXE-install (nodepxe=found, inventory!=found).
        # Flapping such a port interrupts the install -> never converges -> a
        # flap-storm. Gate the NO-LEASE kick on this set below. Best-effort:
        # db.read_installing_ports returns an empty set (never raises) on error,
        # so a failed read degrades to "gate nothing", never crashes the cycle.
        installing = db.read_installing_ports(pool)
        circuit_open = 0
        enq = 0
        for m in mismatches:
            loc = db.resolve_location(pool, m["mac"])
            loc_key = (loc.get("switch"), loc.get("port"))
            if loc_key in steered_ports:
                continue  # the steer's flap already forces re-DHCP
            # BMC-only flap rule: only a BMC mismatch may flap the port. Once the
            # BMC holds its reservation lease it drops out of `mismatches` and the
            # port is never flapped again -- a finished/powered-off host (whose
            # NC-SI sideband keeps the port link UP) no longer drives a futile,
            # SOL-dropping flap. A host (or vm) mismatch is left alone: the host
            # converges onto its own reservation at its next DHCP (every PXE boot
            # / power cycle in triage), not via a disruptive port bounce. NB: the
            # flap of a BMC kick still re-DHCPs everything on the shared port, but
            # we never INITIATE a flap for a host's sake.
            if loc.get("kind") != "bmc":
                log.debug(
                    "skip kick mac=%s kind=%s: only BMC mismatches flap "
                    "(switch=%s port=%s)", m["mac"], loc.get("kind"),
                    loc.get("switch"), loc.get("port"))
                continue
            # Circuit-breaker gate: an auto mismatch whose mac is in cooldown or
            # backoff is not re-enqueued this cycle (operator requests are
            # enqueued elsewhere and bypass this entirely).
            fs = flap_state.get(mismatch_norm(m["mac"]))
            if fs is not None and db.flap_blocked(fs, now,
                                                  self.cfg["kick_cooldown_secs"]):
                if fs.get("backoff_until") is not None and now < fs["backoff_until"]:
                    circuit_open += 1
                continue
            # If there is no active lease, the device may be powered off (absent).
            # Kicking an absent device is futile: run_ladder returns no_rung/failure
            # every sweep, burning attempts until the device is marked stuck.
            # Only kick a no-lease reservation when the port shows link-up (device
            # is present but hasn't DHCPed yet). If the port is link-down/unknown,
            # skip -- the device will be picked up on the next cycle once it powers
            # on and its port comes up.
            if m["lease_ip"] is None:
                # A mid-install host's port must NOT be flapped: the flap
                # interrupts the PXE install so the device never converges. Skip
                # the kick (it will be picked up once install completes and the
                # device DHCPs onto its reservation). Gates ONLY the no-lease
                # branch; a device with a (wrong) lease is unaffected.
                if loc_key in installing:
                    log.debug(
                        "skip kick mac=%s: host mid-install (switch=%s port=%s)",
                        m["mac"], loc.get("switch"), loc.get("port"))
                    continue
                fact = sf_ports.get(loc_key)
                if not db.port_link_up(fact):
                    log.debug(
                        "skip kick mac=%s: no lease and port link not up "
                        "(switch=%s port=%s link=%s)",
                        m["mac"], loc.get("switch"), loc.get("port"),
                        fact.get("link") if fact else "no_fact")
                    continue
            if queue.enqueue(pool, mac=m["mac"], requested_by="auto:mismatch",
                             reason="lease!=reservation",
                             switch=loc.get("switch"), port=loc.get("port"),
                             kind=loc.get("kind")):
                enq += 1
        kicked = 0
        while True:
            req = queue.claim_next(pool)
            if req is None:
                break
            kicked += 1

            # Operator "Reset BMC (Redfish)" request: resolve the BMC IP from
            # this MAC's active lease and fire Manager.Reset (ForceRestart) --
            # NOT the kick/steer ladder. Handled inline before the ladder path.
            if req["reason"] == "operator bmc-reset":
                self._do_bmc_reset(pool, req)
                continue

            # Belt-and-suspenders: an operator-enqueued request (e.g. from the
            # flax-control device page) may carry devices.port in internal short
            # form (et6b1). Canonicalize to Arista so the flap + sentinel match
            # the steer path. Idempotent on already-canonical names.
            flap_port = to_arista(req["port"]) if req["port"] else req["port"]
            # BMC-FW claim guard: a bmc_fw worker holds
            # /run/flax/bmc-fw-active/<port> while it owns the slot's power for
            # a firmware flash. Skip (defer) any action on this port until the
            # flash finishes and the sentinel is removed. Mirrors the
            # install-gate's defer-not-discard approach: the request stays
            # pending so it is retried next cycle once the claim is released.
            # NB: check the INTERNAL form (req["port"], et6b1) -- the filesystem
            # sentinel is keyed on the internal form the triage worker writes
            # (the Arista form's "/" would be a subdir, not a filename).
            claim_port = to_internal(req["port"]) if req["port"] else req["port"]
            if bmc_fw_claim_active(claim_port):
                log.info("skip %s: BMC-FW claim active", claim_port)
                queue.defer(pool, req["id"],
                            cooldown_secs=self.cfg["kick_cooldown_secs"],
                            max_attempts=self.cfg["max_attempts"])
                continue
            # AUTO convergence kick: release the device's STALE Kea lease (a
            # pool/conflict lease whose address differs from its reservation)
            # BEFORE the flap, so the re-DHCP the flap triggers lands on the
            # now-clean reservation instead of Kea renewing the stale lease.
            # Kea analog of reaper-leased's dhcp_release. Self-guarding in db:
            # a lease already matching the reservation (or no reservation) is
            # left alone. NOT done for operator bmc-reset (continue'd above).
            # Covers the auto mismatch kick AND an operator flap: both want a
            # device on a stale pool lease re-homed, and a flap alone only makes
            # the client renew the stale lease (see _release_before_flap).
            if _release_before_flap(req["reason"]):
                released = db.release_stale_lease(pool, req["mac"])
                if released:
                    actions.log_action(
                        pool, switch=req["switch"] or "?", port=flap_port or "?",
                        action="lease_release",
                        detail={"mac": req["mac"], "released": released},
                        outcome="success", reason=req["reason"])
            # The vlan the port is on (from switch_facts) -> the bmc_ll rung's
            # host-side sub-iface via vlan_parents (vid 17 -> eth1.17). Without
            # it run_ladder's `kind=='bmc' and vid` gate is false and EVERY kick
            # falls through to switch_flap -- which can't move a BMC that holds a
            # valid (pool) lease, since a port bounce only makes it renew. With
            # the vid, bmc_ll SSHes the BMC over its v6-LL and bounces eth0 ->
            # fresh DHCP -> lands on the reservation. None (port down / not
            # access) just degrades to the switch_flap backstop, as before.
            kick_vid = (sf_ports.get((req["switch"], flap_port)) or {}).get("access_vid")
            rung, ok = self.ladder(
                pool=pool, switches=self.switches, mac=req["mac"],
                kind=req["kind"], switch=req["switch"], port=flap_port,
                vid=kick_vid, target_ip=None, obmc_user=self.obmc_user,
                obmc_pass=self.obmc_pass, host_creds=self.host_creds,
                flap_hold_seconds=self.cfg["flap_hold_seconds"],
                reason=req["reason"], vlan_parents=self.vlan_parents)
            actions.log_action(
                pool, switch=req["switch"] or "?", port=flap_port or "?",
                action=rung or "no_rung",
                detail={"mac": req["mac"], "rung": rung, "request_id": req["id"]},
                outcome="success" if ok else "failure", reason=req["reason"])
            if ok:
                queue.complete(pool, req["id"], outcome=rung)
                # Circuit-breaker accounting: only AUTO mismatch flaps count.
                # `ok` means the flap EXECUTED, not that the device converged --
                # convergence is detected next cycle (the mac drops out of
                # `mismatches` and clear_flap_state removes its row). Operator
                # requests (bmc-reset handled above; any non-auto reason) are a
                # human explicitly asking and must NOT feed the circuit.
                if req["reason"] == "lease!=reservation":
                    newly = db.record_flap(
                        pool, req["mac"],
                        threshold=self.cfg["flap_circuit_threshold"],
                        window_secs=self.cfg["flap_circuit_window_secs"],
                        backoff_secs=self.cfg["flap_circuit_backoff_secs"])
                    if newly:
                        _emit_circuit_fault(pool, req)
            else:
                queue.defer(pool, req["id"],
                            cooldown_secs=self.cfg["kick_cooldown_secs"],
                            max_attempts=self.cfg["max_attempts"])
        return {"steered": len(steered_ports), "refused": refused,
                "enqueued": enq, "kicked": kicked, "mismatches": len(mismatches),
                "circuit_open": circuit_open, "reclaimed": reclaimed}

    def _do_steer(self, pool, s: dict) -> bool:
        """Sentinel-first set_access_vlan + flap. The flap is the re-DHCP that
        lands the device in 172.<desired_vid> on its (classify-written)
        reservation. Refuses are decided upstream in steer.compute_steers.

        Returns True only when set_access_vlan AND flap both succeeded; False on
        missing driver or any exception. The caller gates steered_ports on this
        so that a failed steer does not suppress the PASS 2 kick fallback.
        """
        sw = self.switches.get(s["switch"])
        if sw is None:
            actions.log_action(pool, switch=s["switch"], port=s["port"],
                action="set_vlan", detail=s, outcome="failure",
                reason="no_driver_for_switch")
            return False
        hold = self.cfg["flap_hold_seconds"]
        sentinel.write_sentinel(pool, switch=s["switch"], port=s["port"],
                                hold_seconds=hold, reason="vlan_steer", mac=None)
        # Mark the flap in-flight BEFORE the shutdown so a process death between
        # flap()'s shutdown and no-shutdown batches is recovered by the startup
        # self-heal / SIGTERM handler. Cleared only on a clean success.
        try:
            db.mark_flap_pending(pool, switch=s["switch"], port=s["port"], mac=None)
        except Exception:
            log.warning("flap-pending mark failed for %s/%s; steering anyway",
                        s["switch"], s["port"])
        try:
            sw.set_access_vlan(s["port"], s["desired_vid"])
            sw.flap(s["port"], hold_seconds=hold)
            outcome, reason = "success", None
        except Exception as e:
            outcome, reason = "failure", str(e)
        if outcome == "success":
            try:
                db.clear_flap_pending(pool, switch=s["switch"], port=s["port"])
            except Exception:
                log.warning("flap-pending clear failed for %s/%s",
                            s["switch"], s["port"])
        actions.log_action(pool, switch=s["switch"], port=s["port"],
            action="set_vlan",
            detail={"desired_vid": s["desired_vid"], "current_vid": s["current_vid"]},
            outcome=outcome, reason=reason)
        return outcome == "success"


    def _do_bmc_reset(self, pool, req: dict) -> None:
        """Operator BMC-reset: resolve the bmc MAC's active lease IP, fire a
        Redfish Manager.Reset, log a reconcile_actions row, and complete/defer.

        The reconcile_requests row carries the bmc MAC; the Redfish reset
        targets the BMC at its current leased address. A missing lease (BMC not
        currently DHCPed) or a failed/unauthenticated reset defers the request
        for retry (same cooldown + attempt-cap backoff as a failed kick).
        """
        bmc_ip = db.lease_ip_for_mac(pool, req["mac"])
        if not bmc_ip:
            ok, detail = False, "no active lease for bmc mac"
        else:
            ok, detail = self.bmc_reset_fn(bmc_ip, self.redfish_creds)
        actions.log_action(
            pool, switch=req["switch"] or "?",
            port=(to_arista(req["port"]) if req["port"] else req["port"]) or "?",
            action="bmc_reset",
            detail={"mac": req["mac"], "bmc_ip": bmc_ip, "detail": detail,
                    "request_id": req["id"]},
            outcome="success" if ok else "failure", reason=req["reason"])
        if ok:
            queue.complete(pool, req["id"], outcome="bmc_reset")
        else:
            queue.defer(pool, req["id"],
                        cooldown_secs=self.cfg["kick_cooldown_secs"],
                        max_attempts=self.cfg["max_attempts"])


def _emit_circuit_fault(pool, req: dict) -> None:
    """Write ONE audit.events fault when a mac's flap circuit opens.

    Mirrors _emit_fault's direct INSERT (service='flax-reconcile', the
    audit.events columns from migration 004) but with kind='flap_circuit_open'
    and the offending mac/switch/port + a reason in the payload. Called exactly
    once per circuit-open (record_flap returns the newly-faulted edge).
    """
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO audit.events (service, kind, mac, switch, port, payload) "
            "VALUES ('flax-reconcile', 'flap_circuit_open', %s, %s, %s, %s)",
            (req["mac"], req["switch"], req["port"],
             json.dumps({"reason": "flap circuit opened: repeated "
                         "non-converging auto flaps",
                         "request_id": req["id"]})))


def _emit_fault(pool, s: dict) -> None:
    """Write an audit.events row so the operator sees a refused steer.

    Matches the real audit.events columns (service, kind, mac, switch, port,
    payload) created in migration 004 and used by flax_observe.persistence.
    emit_audit_event. flax_observe's helper pins service to 'flax-observe' and
    reads its own module-global pool, so we issue the INSERT directly here with
    service='flax-reconcile' and the injected pool.
    """
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO audit.events (service, kind, mac, switch, port, payload) "
            "VALUES ('flax-reconcile', 'steer_refused', %s, %s, %s, %s)",
            (None, s["switch"], s["port"],
             json.dumps({"desired_vid": s["desired_vid"],
                         "current_vid": s["current_vid"],
                         "reason": s["reason"]})))
