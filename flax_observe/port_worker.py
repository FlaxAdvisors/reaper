"""Per-port worker thread for flax-observe.

Wraps the lifted port_worker_one_iter with Postgres persistence + audit
event writes. One thread per port -- matches switchportrecond's pattern of
'a stalled BMC poll on one port does not block any other port'.
"""
import datetime
import json
import logging
import re
import threading
import types
from typing import Any


def _internal_to_arista(port: str) -> str:
    """et6b1 → Ethernet6/1 (inverse of switchportrecond.arista_port_to_internal).

    flax-switch-sense publishes switch_facts.ports keyed by Arista canonical
    names (Ethernet6/1). Geometry uses internal short form (et6b1) — the same
    format switchportrecond's port worker has always consumed. Translate at
    cache-lookup time so the state machine sees the same shape it used to.
    """
    m = re.match(r"^et(\d+)b(\d+)$", port)
    if not m:
        return port
    return f"Ethernet{m.group(1)}/{m.group(2)}"


def _arista_to_internal(port: str) -> str:
    """Ethernet10/2 → et10b2 (inverse of _internal_to_arista; mirrors
    switchportrecond.arista_port_to_internal).

    switch_facts.ports keys are Arista canonical long form (Ethernet10/2);
    geometry/observe_state use internal short form (et10b2). Non-breakout or
    non-Arista names (Ethernet1, swp6) pass through lowercased.
    """
    m = re.match(r"^Ethernet(\d+)/(\d+)$", port)
    if not m:
        return port.lower().replace(" ", "")
    return f"et{m.group(1)}b{m.group(2)}"

from .bmc_probe import (
    probe_bmc_kind, bmc_power_and_sdr_traditional,
    chassis_serial_traditional, chassis_serial_openbmc,
)
from .db import get_pool
from .host_probe import (lookup_lease_ip, lookup_kea_ip, nginx_pxe_seen,
                         inventory_status, ssh_uptime, kea_lease_fresh)
from .ipmi import _default_ipmi_runner
from .persistence import upsert_observe_state, emit_audit_event
from .state_machine import STATE_VARS, port_worker_one_iter, _forget_identity, _forget_port_requested
from .switch_facts import SwitchFactsCache


log = logging.getLogger("flax-observe.port_worker")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def boot_validate_latch(port_state, *, prior_nic_mac, prior_chassis_sn,
                        emit_event):
    """One-shot post-hydration invariant check for a restored inventory latch.

    Runs once, right after the first re-observation of a hydrated worker whose
    linkstate is up. De-latches (re-inventory) only on an OBSERVED violation:

      (B) a freshly classified nic_mac that differs from the prior one, or
      (C) a freshly READ chassis serial that differs from the prior one.

    A signal that wasn't re-observed this cycle (nic_mac / chassis_sn is None,
    or the chassis_sn latch merely held its prior value through a transient BMC
    probe failure) is "couldn't observe", NOT "differs" -- the latch is held.
    linkstate (A) is owned by the normal debounced down-handler, not here.

    Returns "restored" or "dropped" for the caller's logging.
    """
    fresh_nic = port_state.get("nic_mac")
    fresh_sn = port_state.get("chassis_sn")
    reason = None
    if fresh_nic is not None and fresh_nic != prior_nic_mac:
        reason = "nic_mac_changed"
    elif (prior_chassis_sn is not None and fresh_sn is not None
          and fresh_sn != prior_chassis_sn):
        reason = "chassis_sn_changed"

    if reason:
        _forget_identity(port_state, emit_event)
        emit_event({
            "kind": "inventory_latch_dropped",
            "switch": port_state["switch"], "port": port_state["port"],
            "reason": reason,
            "prior_nic_mac": prior_nic_mac, "fresh_nic_mac": fresh_nic,
            "prior_chassis_sn": prior_chassis_sn, "fresh_chassis_sn": fresh_sn,
        })
        return "dropped"

    emit_event({
        "kind": "inventory_latch_restored",
        "switch": port_state["switch"], "port": port_state["port"],
    })
    return "restored"


def load_vlan_parents(vlans_path: str) -> dict:
    """Parse vlans.json -> {vid (int): parent_iface (str)}.

    vlans.json is a list of {vid, parent, ...} dicts (the same schema
    flax_reconcile.__main__._load_vlan_parents reads). Only entries with a
    "parent" key are included -- management/untagged VLANs typically have no
    parent iface. Missing file -> empty dict (no IPv6-LL iface map, so the BMC
    reach falls back to the IPv4-lease path everywhere). Malformed -> fatal so
    a typo surfaces at startup rather than silently disabling LL reach.

    The vid -> iface map lets the observe state machine reach a port's OpenBMC
    at fe80::EUI64%<parent>.<vid> over IPv6 link-local before any IPv4 lease.
    """
    try:
        with open(vlans_path) as f:
            entries = json.load(f)
    except FileNotFoundError:
        log.info("no %s; vlan_parents will be empty (IPv6-LL BMC reach off)",
                 vlans_path)
        return {}
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError("malformed vlans file " + vlans_path + ": " + str(e)) from e
    return {entry["vid"]: entry["parent"] for entry in entries if "parent" in entry}


def make_env(geometry: list[dict], credentials: dict, bmc_credentials: dict,
             host_credentials: dict,
             vlan_parents: dict | None = None,
             macmath_by_vid: dict | None = None,
             redfish_credentials: list | None = None) -> types.SimpleNamespace:
    """Build the env namespace port_worker_one_iter expects.

    vlan_parents maps vid (int) -> bang parent iface (str); the state machine
    uses it to derive the VLAN sub-interface for IPv6 link-local BMC reach.
    Defaults to an empty dict (LL reach disabled, IPv4-lease path only).

    macmath_by_vid maps vid (int) -> macmath config dict (from
    flax_switch_sense.macmath.load_macmath_dir); the port classifier consults
    the entry for a port's access_vid to override the legacy +/-2 BMC<->NIC
    pairing for known hardware families. Defaults to an empty dict (keyword-
    only) so every port gets macmath=None -> legacy classification, leaving
    observe behavior unchanged for vids without a config.
    """
    env = types.SimpleNamespace()
    env.geometry = geometry
    env.credentials = credentials
    env.bmc_credentials = bmc_credentials
    env.host_credentials = host_credentials
    # Redfish creds (credentials-redfish.json, rfuser/rfpass) for probe_bmc_kind's
    # Redfish identification path -- distinct from bmc_credentials (IPMI/openbmc).
    env.redfish_credentials = redfish_credentials or []
    env.vlan_parents = vlan_parents or {}
    env.macmath_by_vid = macmath_by_vid or {}
    env.ipmi_runner = _default_ipmi_runner
    env.probe_bmc_kind = probe_bmc_kind
    env.bmc_power_and_sdr_traditional = bmc_power_and_sdr_traditional
    env.chassis_serial_traditional = chassis_serial_traditional
    env.chassis_serial_openbmc = chassis_serial_openbmc
    # Kea owns DHCP now (Plan 5.6): resolve MAC->IP from Kea's Postgres
    # backend (kea.lease4 live lease, then kea.hosts reservation) instead of
    # the retired dnsmasq.leases / dhcp-hosts files. The state machine calls
    # env.lookup_lease_ip(leases_path, mac, dhcp_hosts_dir) -- keep that
    # 3-arg shape (a named wrapper, not a lambda, so the signature is clear);
    # the path args are accepted-and-ignored.
    def _kea_lease_ip(_leases_path, mac, _dhcp_hosts_dir=None):
        return lookup_kea_ip(get_pool(), mac)
    env.lookup_lease_ip = _kea_lease_ip
    def _kea_lease_fresh(mac, boundary):
        return kea_lease_fresh(get_pool(), mac, boundary)
    env.kea_lease_fresh = _kea_lease_fresh
    env.nginx_pxe_seen = nginx_pxe_seen
    env.inventory_status = inventory_status
    env.ssh_uptime = ssh_uptime
    return env


class PortWorker(threading.Thread):
    """Per-(switch, port) worker. One PortWorker per geometry entry."""

    def __init__(self, switch: str, port: str, ou: str,
                  cache: SwitchFactsCache, env: types.SimpleNamespace,
                  *, cycle_secs: float = 10.0, prior: dict | None = None):
        super().__init__(name=f"port-{switch}-{port}", daemon=True)
        self.switch = switch
        self.port = port
        self.ou = ou
        self.cache = cache
        self.env = env
        self.cycle_secs = cycle_secs
        self._stop_event = threading.Event()
        # Initial port_state -- mirrors switchportrecond's new_port_state()
        # so the lifted state machine finds every expected field. All 12
        # STATE_VARS pre-populated so _set_var doesn't KeyError; per-var
        # resolved fields seeded to their defaults.
        self.port_state: dict[str, Any] = {
            "switch": switch, "port": port, "ou": ou,
            "vars": {v: {"value": "unknown", "since": None} for v in STATE_VARS},
            "bmc_mac": None,
            "bmc_ip": None,
            "nic_mac": None,
            "nic_ip": None,
            "nic_macs": [],
            "junk_macs": [],
            "classification_source": None,
            "lldp_disagreement": False,
            "chassis_sn": None,
            "bmc_power": "0 W",
            "last_polled": None,
            "bmcpower_unknown_streak": 0,
            "bmcpower_stale_since": None,
            "snapshot": {},
        }
        self.last_generation: int | None = None
        self.last_error: str | None = None
        # Boot-validation guard state. Default: nothing to validate (cold start
        # or a prior row with no found+link latch). _hydrate may arm it.
        self._boot_checked: bool = True
        self._boot_prior_nic_mac: str | None = None
        self._boot_prior_chassis_sn: str | None = None
        if prior:
            self._hydrate(prior)

    def _hydrate(self, prior: dict) -> None:
        """Seed port_state from a persisted observe_state row so a restart does
        not blank the inventory latch. Hydrating vars makes the first cycle's
        'link' fact a no-op (no spurious linkstate transition), preserving
        linkstate.since and the inventory boundary. The guard is armed only when
        there is a found+link latch worth protecting; the actual keep/drop
        decision happens in boot_validate_latch after the first re-observation.
        """
        pvars = prior.get("vars") or {}
        presolved = prior.get("resolved") or {}
        if not pvars:
            return
        # Overlay persisted vars onto the fully-seeded defaults rather than
        # replacing wholesale, so every STATE_VAR stays present (a persisted
        # row always carries all 12, but a partial prior must not strand
        # _set_var with a missing key).
        self.port_state["vars"].update(pvars)
        # bmcpower is a live-probe value, re-read every cycle -- do NOT carry a
        # persisted on/off across a restart. If it hydrated, the first
        # post-restart cycle could present a spurious off->on and trip the
        # power-on inventory reset (state_machine) for a node that already booted
        # during the downtime, stranding it at notfound. Leaving it unknown means
        # that reset only fires on a LIVE-observed off->on.
        self.port_state["vars"]["bmcpower"] = {"value": "unknown", "since": None}
        self.port_state["nic_mac"] = presolved.get("nic_mac")
        self.port_state["bmc_mac"] = presolved.get("bmc_mac")
        self.port_state["chassis_sn"] = presolved.get("chassis_sn")
        self.port_state["product_name"] = presolved.get("product_name")
        lss = presolved.get("link_session_since")
        if lss:
            self.port_state["link_session_since"] = lss
        inv = (pvars.get("inventory") or {}).get("value")
        link = (pvars.get("linkstate") or {}).get("value")
        if inv == "found" and link == "link":
            self._boot_checked = False
            self._boot_prior_nic_mac = presolved.get("nic_mac")
            self._boot_prior_chassis_sn = presolved.get("chassis_sn")

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._cycle_once()
            except Exception as e:
                log.exception("port worker cycle failed for %s/%s",
                              self.switch, self.port)
                self.last_error = str(e)
            self._stop_event.wait(self.cycle_secs)

    def _cycle_once(self) -> None:
        # Build the switch_facts view the state machine expects:
        # {(switch, port): fact_dict}.
        # switch_facts is keyed by Arista canonical names (Ethernet6/1) per
        # what flax-switch-sense publishes; geometry uses internal short
        # form (et6b1). Translate at lookup time and reverse-translate the
        # fact's key so the state machine sees its expected port name.
        cache_key_port = _internal_to_arista(self.port)
        raw = self.cache.get_port(self.switch, cache_key_port) or {}
        # flax-switch-sense publishes 'link' (Plan 2 schema); the lifted
        # state machine expects 'linkstate' (switchportrecond schema).
        # Translate at this boundary so the lift stays unmodified.
        fact = dict(raw)
        if "link" in fact and "linkstate" not in fact:
            fact["linkstate"] = fact["link"]
        switch_facts = {(self.switch, self.port): fact}

        # Upstream forget-port signal (e.g. BMC-FW MAC change): forget the whole
        # identity for this port this cycle, superseding the normal probe. The
        # reader consumes (unlinks) the sentinel. Re-classification re-acquires
        # any MAC still on the port next cycle. The dir is overridable via the
        # env for tests; production uses the FORGET_PORT_DIR default.
        _fp_dir = getattr(self.env, "forget_port_dir", None)
        if _forget_port_requested(self.port, forget_port_dir=_fp_dir):
            forget_events: list[dict] = []
            _forget_identity(self.port_state, forget_events.append)
            self._persist()
            for ev in forget_events:
                emit_audit_event(kind=ev.get("kind", "transition"),
                                 switch=self.switch, port=self.port,
                                 mac=ev.get("mac"), payload=ev)
            self.last_error = None
            return

        # Collect transition events emitted during the iter
        events: list[dict] = []
        port_worker_one_iter(
            self.port_state, switch_facts,
            emit_event=events.append, env=self.env,
        )

        # One-shot restart boot validation: a hydrated found+link latch is
        # provisional until this first re-observation. Run only once and only
        # once the port is actually up (linkstate != link is owned by the
        # debounced down-handler inside the iter above).
        if (not self._boot_checked
                and self.port_state["vars"]["linkstate"]["value"] == "link"):
            boot_validate_latch(
                self.port_state,
                prior_nic_mac=self._boot_prior_nic_mac,
                prior_chassis_sn=self._boot_prior_chassis_sn,
                emit_event=events.append,
            )
            self._boot_checked = True

        self._persist()
        # Emit each transition event
        for ev in events:
            emit_audit_event(
                kind=ev.get("kind", "transition"),
                switch=self.switch, port=self.port,
                mac=ev.get("mac"),
                payload=ev,
            )
        self.last_error = None

    def _persist(self) -> None:
        # Persist. vars carries state-machine flags; resolved carries the
        # scalar values triage_compat needs to show actual MAC/IP/SN/power
        # in the Triage UI instead of "found"/"unknown" labels.
        self.port_state["last_polled"] = _now_iso()
        resolved = {
            k: self.port_state.get(k) for k in (
                "bmc_mac", "bmc_ip", "nic_mac", "nic_ip",
                "chassis_sn", "bmc_power", "product_name",
            )
        }
        # Persist the inventory boundary so a restart can restore it exactly
        # (it lives only in memory otherwise; resolved is free-form JSONB, no
        # migration). triage_compat reads only specific keys, so this is inert
        # to the UI.
        resolved["link_session_since"] = self.port_state.get("link_session_since")
        # bmc_kind_cached is the probe-result dict {kind, creds_used, ...};
        # persist only the kind string (never creds_used) under the clean key.
        resolved["bmc_kind"] = (self.port_state.get("bmc_kind_cached") or {}).get("kind")
        # redfish_version: the BMC's Redfish service version (from the unauth
        # service root). bmc-fw uses the low OEM AMI version to recognise an
        # un-updatable board. Host-power-independent; None for non-redfish BMCs.
        resolved["redfish_version"] = (
            self.port_state.get("bmc_kind_cached") or {}).get("redfish_version")
        # role_source is the confirm_roles verdict provenance (heuristic |
        # probe_confirmed | probe_promote_bmc | probe_flip_host). It MUST be
        # persisted so the post lane (flax_classify.post_reserve) can key its
        # probe-confirmed reservation on it -- without this, post_reserve's
        # probe override never fires (resolved had no 'source' key).
        resolved["source"] = self.port_state.get("role_source")
        self.last_generation = upsert_observe_state(
            switch=self.switch, port=self.port,
            vars=self.port_state["vars"],
            last_polled=self.port_state["last_polled"],
            resolved=resolved,
        )
