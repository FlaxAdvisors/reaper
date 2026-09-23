"""flax-discover's stateful derive-and-write cycle.

Reads observe_state (product_name) + switch_facts (junk_macs) + existing
devices, derives each port's family (latch + back-off), enumerates VMs and
assigns a stable per-port vm_n, and upserts the devices table.

Stateful across cycles: the loaded family-map (+ its dir mtime), the back-off
tracker, the last product_name seen per MAC (to reset back-off when it
changes), and the latch-mismatch clock (_contra).

See spec §4.2 -- AMENDED 2026-09-23: `family` is no longer write-once. It is
latched per MAC, but the BMC's MAC comes out of the mezzanine CARD, so a card
moved between platforms used to drag its old family onto the new blade and
steer it to the wrong VLAN. A SUSTAINED family mismatch now breaks the latch;
relocation and absence deliberately do not. See flax-state-delta.md entry 4.
"""
import logging
import os
import re

from .db import read_observe_rows, read_switch_facts, read_devices
from .persistence import upsert_device, next_vm_n, supersede_port, sweep_vacant_port
from .family_map import load_family_map_dir, match_family
from .vm import is_vm_mac, normalise_mac
from .backoff import MatchBackoff


log = logging.getLogger("flax-discover.cycle")


def _internal_to_arista(port: str) -> str:
    """et6b1 -> Ethernet6/1 (switch_facts is keyed by Arista canonical names).
    Mirrors flax_classify.feeder._internal_to_arista."""
    m = re.match(r"^et(\d+)b(\d+)$", port)
    return f"Ethernet{m.group(1)}/{m.group(2)}" if m else port


def _dir_mtime(path: str):
    """Newest mtime across the family-map dir + its *.txt files (so adding,
    editing, or removing a file all register). None if the dir is absent."""
    try:
        mtimes = [os.path.getmtime(path)]
    except OSError:
        return None
    for name in os.listdir(path):
        if name.endswith(".txt"):
            try:
                mtimes.append(os.path.getmtime(os.path.join(path, name)))
            except OSError:
                continue
    return max(mtimes)


def _is_known(family) -> bool:
    return bool(family) and family != "unknown"


def _port_link(facts: dict, switch: str, port: str):
    return (facts.get(switch, {}).get("ports", {})
            .get(_internal_to_arista(port), {}).get("link"))


def one_sighting_per_anchor(observe: list, facts: dict) -> list:
    """Collapse observe rows so no anchor MAC appears at two ports.

    `devices` is ON CONFLICT (mac), so two rows carrying the same BMC MAC write
    the SAME row -- and this is not hypothetical. When a blade leaves port A,
    observe HOLDS its identity at last-known until the link-down commits
    (DOWN_DEBOUNCE_SECS=30), so for ~30s port A still publishes the old blade's
    bmc_mac and cached product_name while port B publishes the same MAC with the
    new blade's. Left alone, the two rows disagree about family, the last write
    wins in unspecified order (read_observe_rows has no ORDER BY), and the value
    alternates every cycle -- flipping desired_vid and making reconcile re-steer
    two live ports, in a loop that self-feeds through the generation-bump NOTIFY.

    The held row is the one whose port no longer has link, so prefer the linked
    sighting. Port name breaks a tie, only so the result cannot depend on row
    order.
    """
    best: dict = {}
    passthrough = []
    for row in observe:
        resolved = row.get("resolved") or {}
        anchor = resolved.get("bmc_mac") or resolved.get("nic_mac")
        if not anchor:
            passthrough.append(row)     # nothing to collide on
            continue
        anchor = normalise_mac(anchor)
        rank = (1 if _port_link(facts, row["switch"], row["port"]) == "link"
                else 0, row["port"])
        if anchor not in best or rank > best[anchor][0]:
            best[anchor] = (rank, row)
    return passthrough + [r for _, r in best.values()]


class Discoverer:
    def __init__(self, family_map_dir: str, base_secs: float, max_secs: float,
                 vacancy_debounce_secs: float = 600.0,
                 contra_debounce_secs: float = 300.0):
        self.family_map_dir = family_map_dir
        self.backoff = MatchBackoff(base_secs, max_secs)
        self.vacancy_debounce_secs = vacancy_debounce_secs
        # How long a DIFFERENT known family must persist before it breaks the
        # latch. WALL CLOCK, not a cycle count: cycles fire on LISTEN with a
        # ~1.5s coalescing window, not only on the poll timer, so "twice
        # running" can be 1.5s apart and would filter almost nothing.
        self.contra_debounce_secs = contra_debounce_secs
        self._fm: dict = {}
        self._fm_mtime = None
        # mac -> (contradicting product_name, when it was FIRST seen). Reset
        # whenever the reading changes, so only a sustained disagreement counts
        # and an oscillating one never accumulates. See _latch_broken.
        self._contra: dict = {}
        # mac -> last product_name, to reset back-off when the reading changes.
        # Mostly UNKNOWN-family MACs, since a known family short-circuits in
        # _derive_family before this write -- but a MAC whose latch has just
        # been broken by a sustained mismatch also lands here for the cycle it
        # takes to re-derive. Bounded by the port count either way.
        self._last_pn: dict = {}

    def _maybe_reload_family_map(self) -> None:
        mtime = _dir_mtime(self.family_map_dir)
        if mtime != self._fm_mtime:
            self._fm = load_family_map_dir(self.family_map_dir)
            self._fm_mtime = mtime
            self.backoff.clear_all()
            log.info("family-map reloaded (%d families)", len(self._fm))

    def _latch_broken(self, mac, current, product_name, now) -> bool:
        """Does the blade's own FRU sustainedly disagree with the latch?

        The latch is keyed by MAC, but on these blades the BMC's MAC comes out
        of the MEZZANINE CARD's own MAC block -- so it identifies the CARD, not
        the blade. A card harvested from a tiogapass and seated in a leopard
        drags `family=tiogapass` with it, and classify then steers the leopard
        onto vid 17 (measured live on et9b2, 2026-09-23: one devices row holding
        both `family tiogapass` and `product_name Leopard ORv2-DDR4`).

        RELOCATION IS DELIBERATELY NOT A TRIGGER (operator decision
        2026-09-23). The latch surviving a move is what keeps a blade
        identifiable while it is being moved and its BMC has not come back on a
        stable address. Absence is not a trigger either: run_one_cycle treats an
        empty poll as no-data and never wipes a port on it. Only positive,
        sustained disagreement counts.
        """
        # Only a KNOWN, different family is evidence. product_name is empty or
        # unmapped while a BMC is still coming up -- that is what the derive
        # back-off below exists for, and it is not a mismatch.
        observed = match_family(self._fm, product_name)
        if not (_is_known(observed) and observed != current):
            self._contra.pop(mac, None)
            return False
        # Debounce on wall clock (flax-state-ref.md: "transitions that would
        # clear or reverse a latch are debounced -- required to persist for a
        # minimum duration"). Breaking the latch re-steers a live port and
        # re-IPs a blade, so one bad FRU read must not do it.
        seen_pn, first_seen = self._contra.get(mac, (None, None))
        if seen_pn != product_name:
            self._contra[mac] = (product_name, now)     # (re)start the clock
            return False
        if now - first_seen >= self.contra_debounce_secs:
            self._contra.pop(mac, None)
            return True
        return False

    def _derive_family(self, mac, product_name, existing_latched, now) -> str:
        """Latch + back-off. Returns the family to write."""
        current = existing_latched.get("family")
        if _is_known(current) and not self._latch_broken(mac, current,
                                                         product_name, now):
            return current
        if self._last_pn.get(mac) != product_name:
            self.backoff.reset(mac)
            self._last_pn[mac] = product_name
        if not self.backoff.due(mac, now):
            return current or "unknown"
        family = match_family(self._fm, product_name)
        if family:
            self.backoff.forget(mac)
            return family
        self.backoff.record_miss(mac, now)
        # `current` is necessarily not-known here: a known family either returned
        # early above, or broke the latch -- and a break requires match_family to
        # have hit, so the call above cannot have missed. So this never
        # downgrades a known family (which would not steer, but WOULD rewrite the
        # reservation hostname to `unknown...` and defeat classify's bmc-only
        # host suppression).
        return "unknown"

    def run_one_cycle(self, pool, now: float) -> dict:
        self._maybe_reload_family_map()
        observe = read_observe_rows(pool)
        facts = read_switch_facts(pool)
        existing = read_devices(pool)
        observe = one_sighting_per_anchor(observe, facts)

        vm_by_port: dict = {}
        latched_by_mac: dict = {}
        # The bmc MAC we last recorded at each (switch, port). Used to detect a
        # genuine BMC-change event (one BMC per port): supersede fires ONLY when
        # a different bmc arrives, never on a transient switch-poll gap.
        existing_bmc_by_port: dict = {}
        for d in existing:
            latched_by_mac[d["mac"]] = d["latched"]
            if d["kind"] == "bmc":
                existing_bmc_by_port[(d["switch"], d["port"])] = d["mac"]
            if d["kind"] == "vm":
                # vm_n is keyed per (switch, port); braintree is Kea pool-mode
                # (one access vid per port), so this matches the spec's
                # per-(port,vid) intent without storing vid. (family->vid is
                # out of scope.)
                vm_by_port.setdefault((d["switch"], d["port"]), {})[d["mac"]] = \
                    d["latched"].get("vm_n")

        written = 0
        superseded = 0
        for row in observe:
            sw, port = row["switch"], row["port"]
            resolved = row.get("resolved") or {}
            bmc_mac = resolved.get("bmc_mac")
            nic_mac = resolved.get("nic_mac")
            bmc_mac = normalise_mac(bmc_mac) if bmc_mac else None
            nic_mac = normalise_mac(nic_mac) if nic_mac else None
            # Need at least one positive occupant to act. A fully-empty resolved
            # (no bmc AND no nic) is a transient/no-data poll -- skip it; never
            # wipe a port on absence. A port that resolved to HOST-ONLY
            # (bmc_mac=None, nic_mac set -- e.g. probe_flip_host, or the BMC is
            # simply dark) IS actionable: enroll the host and sweep stale rows,
            # so an observe role-flip propagates here instead of freezing the
            # port at its pre-flip snapshot.
            if not bmc_mac and not nic_mac:
                continue
            product_name = resolved.get("product_name")

            # This cycle's chassis at (sw, port): the bmc (if any), its paired
            # host nic, and the VMs we assign below. Rows at the port whose mac
            # is NOT in this set belong to a superseded occupant.
            keep_macs = []

            # Family is latched per-chassis; anchor on the bmc when present,
            # else the nic (a host-only port still has a derivable family).
            anchor = bmc_mac or nic_mac
            prior = latched_by_mac.get(anchor, {})
            family = self._derive_family(anchor, product_name, prior, now)

            if bmc_mac:
                keep_macs.append(bmc_mac)
                bmc_latched = {"family": family}
                if resolved.get("chassis_sn"):
                    bmc_latched["serial"] = resolved["chassis_sn"]
                if product_name:
                    bmc_latched["product_name"] = product_name
                for k in ("serial", "product_name"):
                    if k not in bmc_latched and prior.get(k):
                        bmc_latched[k] = prior[k]
                upsert_device(pool, mac=bmc_mac, switch=sw, port=port,
                              kind="bmc", latched=bmc_latched)
                written += 1

            if nic_mac:
                keep_macs.append(nic_mac)
                upsert_device(pool, mac=nic_mac, switch=sw, port=port,
                              kind="host", latched={"family": family})
                written += 1

            port_facts = (facts.get(sw, {}).get("ports", {})
                          .get(_internal_to_arista(port), {}))
            port_vm_map = vm_by_port.setdefault((sw, port), {})
            for entry in port_facts.get("junk_macs", []):
                vm_mac = normalise_mac(entry["mac"])
                if not is_vm_mac(vm_mac):
                    continue
                if vm_mac in port_vm_map and port_vm_map[vm_mac]:
                    vm_n = port_vm_map[vm_mac]
                else:
                    vm_n = next_vm_n(port_vm_map)
                    port_vm_map[vm_mac] = vm_n
                keep_macs.append(vm_mac)
                upsert_device(pool, mac=vm_mac, switch=sw, port=port, kind="vm",
                              latched={"family": family, "vm_n": vm_n})
                written += 1

            # "One BMC per (switch, port)": evict the prior occupant's rows
            # (old bmc + old host + old VMs) on a GENUINE change, keeping just
            # this cycle's chassis. `existing_bmc != bmc_mac` covers a swap
            # (8a->c8) AND a flip to host-only / dark BMC (8a->None) -- the
            # latter is how an observe probe-flip's stale bmc + phantom host get
            # cleaned. First enrollment (no existing bmc) and an unchanged bmc
            # do nothing extra, so a transient poll gap can never wipe a port;
            # keep_macs is also non-empty here (guarded above), so the
            # empty-keep "delete everything at the port" footgun can't fire.
            # flax-classify's stale-delete then drops the orphaned kea.hosts.
            existing_bmc = existing_bmc_by_port.get((sw, port))
            if existing_bmc is not None and existing_bmc != bmc_mac:
                superseded += supersede_port(pool, switch=sw, port=port,
                                             keep_macs=keep_macs)
                # Reflect the swap for any later observe row at this port.
                existing_bmc_by_port[(sw, port)] = bmc_mac

        # Vacancy sweep: a port that has gone link-down past the debounce no
        # longer holds a chassis -- delete its stale devices rows so /devices
        # reflects reality. Gate on the CURRENT switch_facts link == "nolink"
        # (a re-occupied or still-linked port is never swept, including a
        # powered-off-but-link-up NC-SI host); the debounce age test lives in
        # sweep_vacant_port (DB clock on devices.last_seen). Iterate only the
        # ports that actually have devices rows -- others have nothing to sweep.
        # A port absent from switch_facts (poll gap) has link=None != "nolink",
        # so it is left alone.
        swept = 0
        for sw, port in {(d["switch"], d["port"]) for d in existing}:
            link = (facts.get(sw, {}).get("ports", {})
                    .get(_internal_to_arista(port), {}).get("link"))
            if link == "nolink":
                n = sweep_vacant_port(
                    pool, switch=sw, port=port,
                    debounce_secs=self.vacancy_debounce_secs)
                if n:
                    log.info("vacancy sweep: deleted %d devices row(s) at "
                             "%s/%s (link-down > %.0fs)", n, sw, port,
                             self.vacancy_debounce_secs)
                swept += n

        return {"written": written, "superseded": superseded, "swept": swept}
