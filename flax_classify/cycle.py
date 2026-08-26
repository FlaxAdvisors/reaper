"""Single classify cycle: read inputs, derive targets, write proposals,
garbage-collect anything that disappeared.

Run on a timer (default 30s) and also on LISTEN switch_facts / LISTEN
observe_state notifications (Task 8).
"""
import logging

from .db import read_observe_rows, read_switch_facts, read_devices
from .desired_port import upsert_desired_port
from .desired_reservations import upsert_desired, sweep_desired_not_in
from .kea_hosts import read_aliases_for_macs
from .feeder import derive_targets, _internal_to_arista
from .formula import classify_one
from .dns_hosts import write_hosts_file


log = logging.getLogger("flax-classify.cycle")


def run_one_cycle(pool, *, fp_to_vid=None, geom_tokens=None,
                  no_steer=None, bmc_only=None, resolve=None,
                  dns_hosts_path="/etc/dnsmasq.hosts/flax-triage-devices") -> dict:
    """Read observe_state + switch_facts, classify, write desired rows, sweep
    stale ones. Returns counts: {written, deleted, skipped, written_desired,
    purged}.

    fp_to_vid / geom_tokens / no_steer drive the feeder's steering policy.
    They default to empty (no steering: every known family holds its current
    access_vid, nothing is excluded) so the cycle stays callable; CL4 wires
    the real loads from the site files and passes them down.

    resolve: optional role-registry (switch, port) -> role|None callable
    (flax_classify.role_registry, wired up in __main__). Passed straight
    through to derive_targets; None (default) preserves the legacy
    geom_tokens/phase_for path byte-identically.

    Post-3b demolition: this cycle writes desired_reservations ONLY -- it
    never touches kea.hosts / kea.ipv6_reservations directly anymore. The
    materializer (flax_classify.materializer.run_cycle) is now the sole
    triage kea writer, reading this cycle's desired_reservations snapshot
    and reconciling it against kea's actuals on its own pass. `deleted`
    below is the count of rows `sweep_desired_not_in` removed from
    desired_reservations for owner_role="triage" this cycle (the
    materializer notices the disappearance and deletes/updates the
    corresponding kea row on ITS next pass).

    Where the retired kea purges' duties went (`purged` is always 0 here,
    kept in the summary shape only for healthz.record_cycle_done compat):
    desired_reservations is keyed uniquely by mac, so on the DESIRED side a
    mac's move updates its own row in place and any other mac's stale row
    is swept by sweep_desired_not_in below -- but kea.hosts keys on
    (mac, type, dhcp4_subnet_id), so the KEA-side dup a subnet/vid-changing
    move creates (purge_relocated_mac_hosts' old job) still needs a
    kea-side cleanup: the APPLY layer does it, calling
    kea_hosts.delete_other_subnet_rows right after each vid-moving upsert
    (see materializer.apply_actions). Cross-mac staleness at a port
    (purge_superseded_port_hosts' old job) converges via this sweep + the
    materializer's delete action. Genuinely ambiguous kea multi-row states
    for one mac (rows split across owners, operator_note-protected dups)
    remain planner-skipped (multi_actual) by policy.
    """
    observe = read_observe_rows(pool)
    facts = read_switch_facts(pool)
    devices = read_devices(pool)
    targets = derive_targets(
        observe, facts, devices,
        fp_to_vid=fp_to_vid or {},
        geom_tokens=geom_tokens or {},   # {switch: set(tokens)} for phase_for
        no_steer=no_steer or set(),
        bmc_only=bmc_only or set(),
        resolve=resolve)

    # --- write desired_port rows (one per port) ----------------------------
    # Group targets by canonical (switch, port_arista). bmc/host on the same
    # port share the same steered vid; vms on the port share it too.
    # occupants shape: {"bmc": <mac>, "host": <mac>, "vms": [<mac>, ...]}
    port_groups: dict = {}  # (switch, port_arista) -> {"vid": int, "occupants": dict}
    for t in targets:
        port_arista = _internal_to_arista(t["port"])
        key = (t["switch"], port_arista)
        if key not in port_groups:
            port_groups[key] = {"vid": t["vid"], "occupants": {}}
        entry = port_groups[key]
        kind = t["kind"]
        if kind == "vm":
            entry["occupants"].setdefault("vms", []).append(t["mac"])
        else:
            entry["occupants"][kind] = t["mac"]

    written_desired = 0
    for (sw, port_arista), info in port_groups.items():
        try:
            upsert_desired_port(pool, switch=sw, port=port_arista,
                                desired_vid=info["vid"],
                                occupants=info["occupants"])
            written_desired += 1
        except Exception as e:
            log.warning("upsert_desired_port failed %s/%s: %s", sw, port_arista, e)

    # --- classify + collect desired_reservations rows (one per mac) --------
    written = 0
    skipped = 0
    # EVERY target mac this cycle, regardless of phase -- the sweep keep-set.
    # The triage sweep is pure vacancy-GC: it evicts an owner="triage" row only
    # when its mac has genuinely vanished from observation, NEVER because the
    # mac merely resolved to another lane this cycle (that lane re-owns it via
    # its own ON CONFLICT write; the materializer's purge_handoff finishes the
    # handoff). Coupling the sweep to the phase-scoped WRITE (seen-only) would
    # actively evict a GENUINE triage reservation the moment its port
    # mis-resolves (e.g. an empty/stale geometry.json dropping it to post's
    # catch_all) -- data loss. Only the write below is phase-scoped.
    present_macs = set()
    # Collected A+AAAA records for the dnsmasq hosts file, one per written
    # target. Rendered + atomically written ONCE after the loops below.
    dns_records: list = []
    # MAC per dns_records entry (parallel list), so operator aliases from
    # user_context can be attached without changing the dns_record shape.
    dns_macs: list = []
    # One desired_reservations kwargs dict per successfully classified
    # target, written in a single batch below (with sweep_desired_not_in)
    # inside one try/except so a mid-batch failure can never partially sweep.
    desired_targets: list = []
    for t in targets:
        present_macs.add(t["mac"])
        # Lane isolation: this is the TRIAGE reservation lane. It only claims
        # ports that resolve to triage (phase carried by the feeder from the
        # switch+port role registry). Ports resolving to another role (post,
        # ...) still got their desired_port steering row above -- reconcile
        # keeps steering them -- but their reservation is owned by that role's
        # own lane (e.g. post_reserve.py, owner_role="post"). Writing
        # owner="triage" here would war with that lane over the mac PK and
        # mis-tag a post-subnet reservation as triage. Skip before classify_one
        # so no owner="triage" row is written for a non-triage port; the post
        # lane's owner="post" write (ON CONFLICT mac) then owns it, and the
        # materializer's purge_handoff flips any stale triage kea row to post.
        # (The mac stays in present_macs above, so the sweep never evicts it.)
        if t.get("phase") != "triage":
            continue
        # Feeder preserves flax-observe's internal port shape (et6b1);
        # formula.classify_one was lifted from reaper_leased and expects
        # Arista's long form for the hostname _port_token to collapse
        # correctly ('Ethernet6/1' -> '6b1'). Normalise at this seam so
        # both sides stay byte-identical to reaper-leased's output.
        port_for_formula = _internal_to_arista(t["port"])
        try:
            r = classify_one(t["switch"], port_for_formula, t["mac"],
                             t["kind"], t["vid"],
                             family=t.get("family", "unknown"),
                             vm_n=t.get("vm_n"))
        except ValueError as e:
            log.warning("classify_one skipped %s/%s mac=%s: %s",
                        t["switch"], t["port"], t["mac"], e)
            skipped += 1
            continue
        target_kwargs = dict(
            switch=t["switch"], port=t["port"], mac=t["mac"],
            kind=t["kind"], vid=t["vid"],
            ipv4_address=r["ipv4_address"], hostname=r["hostname"],
            ipv6_address=r["ipv6_address"])
        desired_targets.append(target_kwargs)
        dns_records.append({"hostname": r["hostname"],
                            "ipv4": r["ipv4_address"],
                            "ipv6": r["ipv6_address"]})
        dns_macs.append(t["mac"])
        written += 1

    # desired_reservations write (the ONLY reservation write this cycle):
    # one row per classified triage target, owner_role="triage", replaying the
    # SAME values just computed above, then sweep any owner="triage" row whose
    # mac is not in present_macs (a genuinely-vacated triage port). A failure
    # here (including UndefinedTable if migration 030 hasn't landed yet --
    # deploy-order safety) is logged and swallowed so it can never break the
    # DNS/aliases tail of the cycle.
    deleted = 0
    try:
        for kw in desired_targets:
            upsert_desired(
                pool, owner_role="triage", mac=kw["mac"], kind=kw["kind"],
                hostname=kw["hostname"], ipv4=kw["ipv4_address"],
                ipv6=kw["ipv6_address"], vid=kw["vid"], switch=kw["switch"],
                port=kw["port"])
        deleted = sweep_desired_not_in(pool, owner_role="triage",
                                       keep_macs=present_macs)
    except Exception:
        log.exception("desired_reservations write failed this cycle")

    # No kea-side purge concept remains in this cycle (see docstring above).
    purged = 0

    # Attach operator-set DNS aliases (user_context.aliases, written by the
    # flax-control WebUI) so they render alongside each device's primary
    # hostname. A read error here MUST NOT kill the cycle or drop the primary
    # records -- aliases are best-effort decoration on top of the reservations.
    try:
        aliases_by_mac = read_aliases_for_macs(pool, dns_macs)
        if aliases_by_mac:
            for rec, mac in zip(dns_records, dns_macs):
                al = aliases_by_mac.get(mac)
                if al:
                    rec["aliases"] = al
    except Exception as e:
        log.warning("read_aliases_for_macs failed: %s", e)

    # Publish device A+AAAA records to dnsmasq. A hosts-file error (disk, perms,
    # missing mount) MUST NOT kill the cycle -- DHCP writes already succeeded.
    try:
        write_hosts_file(dns_hosts_path, dns_records)
    except Exception as e:
        log.warning("write_hosts_file failed (%s): %s", dns_hosts_path, e)

    return {"written": written, "deleted": deleted, "skipped": skipped,
            "written_desired": written_desired, "purged": purged}
