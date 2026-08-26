"""12-var per-port state machine — lifted from scripts/switchportrecond.py.

The function is large (~350 LoC) but tightly cohesive — every line is a
state-variable transition for one of the 12 vars (linkstate, bmcmac,
bmcip, bmcping, bmcipmi, bmcpower, chassissn, nodeip, nodeping, nodepxe,
nodessh, inventory). The bmcpower latch behaviour was added by the
parallel branch that landed on main; it is part of the lift.

I/O contract:
  port_state    — mutable dict, updated in place
  switch_facts  — read-only dict {(switch, port): fact_dict}
                  (Plan 3: comes from SwitchFactsCache, not the old
                   in-memory dict that SwitchFetcher maintained)
  emit_event    — callable(dict) -> None; receives transition records
  env           — SimpleNamespace with probe callables and config paths:

    Probe callables (option a — injected for testability):
      .probe_bmc_kind(ip, credentials, bmc_creds) -> {"kind", ...}
      .bmc_power_status_openbmc(ip, creds_pair)   -> 'on'/'off'/'unknown'
      .bmc_power_and_sdr_traditional(ip, creds_pair) -> (power, watts)
      .chassis_serial_traditional(ip, creds_pair) -> str|None
      .chassis_serial_openbmc(ip, creds_pair)     -> str|None
      .ping_host(ip)                              -> 'ok'/'fail'/'unknown'
      .lookup_lease_ip(path, mac, dhcp_dir)       -> str|None
      .nginx_pxe_seen(log_path, ip)               -> 'found'/'notfound'/'unknown'
      .ssh_uptime(ip, host_creds)                 -> 'ok'/'fail'/'unknown'
      .inventory_status(nodes_root, nic_mac, link_ts) -> 'found'/'notfound'/'unknown'
      .intentional_flap_active(switch, port)      -> bool
      .sol_session_active(bmc_ip)                 -> bool
      .ensure_inband_admin_configured(port_state, bmc_ip, bmc_mac, kind,
                                      creds_used, emit_event)

    Config (plain values, not callables):
      .credentials       dict  — openbmc creds (obmcuser/obmcpass)
      .bmc_credentials   list  — [{bmcuser, bmcpass}, ...]
      .host_credentials  list  — [{user, pass}, ...]
      .leases_path       str   — dnsmasq.leases path
      .dhcp_hosts_dir    str   — dnsmasq static reservations dir
      .nginx_access_log  str   — nginx access log path
      .nodes_root        str   — /export/nodes root

Production callers build this namespace via make_env() (Plan 3 Phase E);
tests stub it out using _stub_env() in the test module.
"""
import datetime
import logging
import os
import subprocess
import time as _time_mod

from flax_observe import ll as ll_mod

log = logging.getLogger("flax-observe.state_machine")


def reach_for_mac(mac, access_vid, vlan_parents, ping6, resolve_ip, timeout=2,
                  ping4=None):
    """Return ``(target, is_ll)`` -- how to reach a MAC's BMC for probing.

    Prefer the **IPv4 address** (``resolve_ip(mac)``) -- it works for BOTH the
    OpenBMC SSH path AND IPMI/RMCP (traditional BMCs). IPMI does NOT work over
    an IPv6 link-local zoned address, so a traditional BMC reached via LL
    mis-probes as ``unknown`` (no kind, no chassis serial / product name).

    But ``resolve_ip`` returns a *reservation* IP (kea.hosts) whether or not
    the device has actually claimed it: a quiet OpenBMC switch that hasn't
    DHCPed yet still has a reservation IP where nothing answers. So only prefer
    the IPv4 when it actually responds to ICMP (``ping4(ip) is True``). When
    the IPv4 does not answer, fall to the IPv6 EUI-64 link-local
    ``fe80::...%<parent>.<vid>`` (SSH works fine over LL -- this is how we
    ``weutil`` an OpenBMC switch before it leases). A non-answering IPv4 means
    there is no traditional BMC there to lose.

    ``ping4`` is ``(ip) -> bool``; when None, the IPv4 is preferred
    unconditionally (back-compat for callers/tests that do not gate on ICMP).
    *ping6* is ``(addr, timeout=) -> bool``; *resolve_ip* is ``(mac) -> ip``.
    """
    ip = resolve_ip(mac)
    if ip and (ping4 is None or ping4(ip)):
        return (ip, False)
    try:
        vid = int(access_vid)
    except (TypeError, ValueError):
        return (ip, False)
    parent = vlan_parents.get(vid)
    if mac and parent:
        iface = parent + "." + str(vid)
        cand = ll_mod.ll_target(mac, iface)
        if ping6(cand, timeout=timeout):
            return (cand, True)
    return (ip, False)

# ---------------------------------------------------------------------------
# Constants (mirrored from scripts/switchportrecond.py)
# ---------------------------------------------------------------------------

SSH_KNOWN_HOSTS = "/opt/flax/var/ssh/known_hosts"
SSH_TIMEOUT_SECS = 8
PING_PACKETS = 1
PING_WAIT_SECS = 1
SOL_ACTIVE_DIR = "/run/flax/sol-active"
INTENTIONAL_FLAP_DIR = "/run/flax/intentional-flap"
FORGET_PORT_DIR = "/run/flax/forget-port"
MAX_FLAP_HOLD_SECS = 120
# Bound on the bmcpower latch: hold a stale on/off through this many consecutive
# unknown polls (transient RMCP+/session-table glitches), then GIVE UP and
# surface 'unknown'. Without a bound, a node that powers off but whose BMC then
# stops answering IPMI (AMI session-table exhaustion -- common right after a
# power-down) held a confidently-wrong "on"/watts on the Triage tile forever.
# After the bound the tile shows '—' (unreadable), never a wrong value.
LATCH_MAX_UNKNOWN_STREAK = 3
BMC_ENABLE_INBAND_ADMIN_BIN = "/opt/flax/bin/bmc-enable-inband-admin"
BMC_ENABLE_INBAND_ADMIN_TIMEOUT_SECS = 30

STATE_VARS = [
    "linkstate", "bmcmac", "bmcip", "bmcping", "bmcipmi",
    "bmcpower", "chassissn", "nodeip", "nodeping", "nodepxe",
    "nodessh", "inventory",
    # multibmc: fault var surfaced by role confirmation when more than one
    # visible MAC on a port probes as a BMC (one-BMC-per-switchport invariant
    # broken). "found" = fault present, "clear" = single/zero confirmed BMC.
    "multibmc",
]

# ---------------------------------------------------------------------------
# MAC classification
# ---------------------------------------------------------------------------
#
# Consolidated onto flax_switch_sense.classify (Task 4): observe used to carry
# a byte-for-byte copy of this decision tree, but switch-sense's copy is now
# the macmath-aware one (Task 2). Re-export it so observe's port classifier
# honors the same per-vid macmath the switch-sense publisher does -- otherwise
# observe_state.nic_mac (which drives the flax-classify host reservation) would
# keep the legacy +/-2 pairing while switch-sense classifies the same port
# correctly. The flax-control image bundles both packages so this import is
# always available at runtime.
#
# classify_macs / ClassifiedMacs stay importable from this module for the
# existing tests and any in-tree callers (the names are aliases now).
from flax_switch_sense.classify import (  # noqa: E402
    classify_macs,
    ClassifiedMacs,
)


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def _ts_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# Inventory de-latch debounce: a real switch-reported link-down must persist at
# least this long before it is treated as a durable link-session break (chassis
# swap / removal) that de-latches inventory. A brief flap -- or a switch-sense
# eAPI poll timeout, which surfaces here as a cache miss and is handled
# separately -- must NOT de-latch. Override per-env via env.down_debounce_secs.
DOWN_DEBOUNCE_SECS = 30


def _secs_since(iso_ts):
    """Seconds elapsed since an ISO-8601 'YYYY-MM-DDTHH:MM:SSZ' timestamp."""
    if not iso_ts:
        return 0.0
    then = datetime.datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")
    return (datetime.datetime.utcnow() - then).total_seconds()


# ---------------------------------------------------------------------------
# State var helpers
# ---------------------------------------------------------------------------

def _set_var(port_state, var, new_value, emit_event):
    """Update a state var; emit a transition event if value changed."""
    cur = port_state["vars"][var]
    if cur["value"] == new_value:
        return False
    emit_event({
        "kind": "transition",
        "switch": port_state["switch"],
        "port": port_state["port"],
        "var": var,
        "from": cur["value"],
        "to": new_value,
    })
    port_state["vars"][var] = {"value": new_value, "since": _ts_now()}
    return True


def _forget_identity(port_state, emit_event):
    """Forget a port's WHOLE identity: advance the inventory boundary, drop the
    chassis-identity latches, AND clear the MAC/IP scalars + blank the BMC and
    node reachability vars.

    Used at every de-latch site -- committed link-down, the restart
    boot-validation guard (a swap detected after hydration), and (once wired in
    a later task) the forget-port sentinel (BMC-FW MAC change). A swap is a swap
    regardless of how it was detected, so all three forget the same way. Unlike
    _advance_inventory_boundary (same-chassis power cycle), this nulls the
    identity scalars so a removed blade leaves no cached MAC/IP/SN on the tile
    and flax-classify can sweep its reservation. Re-classification re-acquires
    any MAC still present on the port.
    """
    # Inventory boundary + chassis-identity latches (the original behaviour).
    port_state["link_session_since"] = _ts_now()
    port_state.pop("bmc_kind_cached", None)
    port_state["chassis_sn"] = None
    port_state["product_name"] = None
    _set_var(port_state, "chassissn", "unknown", emit_event)
    _set_var(port_state, "inventory", "notfound", emit_event)
    # Identity scalars (resolved JSONB / Triage tile) -- the new full wipe.
    port_state["bmc_mac"] = None
    port_state["nic_mac"] = None
    port_state["bmc_ip"] = None
    port_state["nic_ip"] = None
    port_state["nic_macs"] = []
    # Reachability vars are no longer trustworthy once identity is forgotten.
    for v in ("bmcmac", "bmcip", "bmcping", "bmcipmi", "bmcpower", "multibmc",
              "nodeip", "nodeping", "nodepxe", "nodessh"):
        _set_var(port_state, v, "unknown", emit_event)
    port_state["bmc_power"] = "?"
    port_state["bmcpower_unknown_streak"] = 0
    port_state["bmcpower_stale_since"] = None


def _advance_inventory_boundary(port_state):
    """Start a new inventory session WITHOUT touching chassis identity.

    A node power-on (bmcpower off->on) means the host will re-netboot and
    re-collect inventory, so the prior post file is stale -- advance the
    boundary so inventory_status returns notfound until the new inventory
    lands. UNLIKE _forget_identity (committed link-down / chassis swap),
    this does NOT clear chassis_sn / bmc_kind_cached / product_name: a power
    cycle is the SAME chassis. inventory itself is recomputed against the new
    boundary later in the same cycle by the inventory_status block.
    """
    port_state["link_session_since"] = _ts_now()


# Node-side stage names, used to blank every node var to unknown when a port
# has no nic_mac (nothing to probe). These vars are NOT monotonically gated
# against each other -- each is set from its own signal (see port_worker_one_iter):
# a cross-stage "don't skip steps" gate un-latched inventory for idle/off nodes
# (0.9.37/0.9.38). inventory stays a latched fact; nodeip/nodepxe are
# session-relative; nodeping/nodessh are live.
NODE_PIPELINE = ["nodeip", "nodeping", "nodepxe", "nodessh", "inventory"]


def _link_value_from_eapi(linkstate):
    """Map eAPI raw values to canonical, OR pass canonical values through.

    Switchportrecond's switch_fetch_once fed RAW values from Arista
    (`connected`, `notconnect`). flax-switch-sense (Plan 2) does the
    canonical mapping inside the driver and publishes already-canonical
    `link`/`nolink`/`unknown`. This function now accepts both — flax-observe
    can consume either source without a separate translation layer.
    """
    if linkstate in ("link", "nolink", "unknown"):
        return linkstate
    if linkstate == "connected":
        return "link"
    if linkstate in ("notconnect", "disabled"):
        return "nolink"
    return "unknown"


def _persist_junk_macs(port_state, new_junk_list, emit_event):
    """Reconcile port_state['junk_macs'] against the latest classifier
    output. Emit `junk_mac_observed` once on first sighting of a (mac, reason)
    pair; emit `junk_mac_cleared` when a previously-tracked entry is no
    longer in the new list.
    """
    previous = {(j["mac"], j["reason"]): j for j in port_state.get("junk_macs", [])}
    new = {(j["mac"], j["reason"]): j for j in new_junk_list}

    # Disappeared entries
    for key, j in previous.items():
        if key not in new:
            emit_event({
                "kind": "junk_mac_cleared",
                "switch": port_state["switch"],
                "port": port_state["port"],
                "mac": j["mac"],
                "reason": j["reason"],
            })

    # New entries
    persisted = []
    now = _ts_now()
    for key, j in new.items():
        if key in previous:
            persisted.append(previous[key])  # preserve original `since`
        else:
            persisted.append({**j, "since": now})
            emit_event({
                "kind": "junk_mac_observed",
                "switch": port_state["switch"],
                "port": port_state["port"],
                "mac": j["mac"],
                "reason": j["reason"],
            })
    port_state["junk_macs"] = persisted


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

def ping_host(ip, timeout_secs=PING_WAIT_SECS):
    """ICMP ping; returns 'ok' / 'fail' / 'unknown'."""
    if not ip:
        return "unknown"
    try:
        r = subprocess.run(
            ["ping", "-c", str(PING_PACKETS), "-W", str(timeout_secs),
             "-q", ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_secs + 2,
        )
    except subprocess.TimeoutExpired:
        return "fail"
    return "ok" if r.returncode == 0 else "fail"


# ---------------------------------------------------------------------------
# Sentinel helpers
# ---------------------------------------------------------------------------

def _parse_sentinel_kv(path):
    """Parse a key=value sentinel file. Returns {} on any read/parse failure."""
    try:
        with open(path) as f:
            kv = {}
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
            return kv
    except (OSError, ValueError):
        return {}


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def intentional_flap_active(switch, port, intentional_flap_dir=None,
                             now_fn=None):
    """True iff <dir>/<switch>/<port> holds a fresh flap sentinel.

    The file is a small key=value record written by an actor (today:
    reaper-leased's kick_via_switch_flap) BEFORE it intentionally
    bounces the port. While the sentinel is fresh, the consumer must
    freeze per-port state — no linkstate transition recorded, no var
    reset, no IPMI/ssh probe — so that our own deliberate flap does not
    invalidate hard-won observability state (chassis_sn latch,
    bmc_kind_cached, inventory).

    Freshness rule: `until` (unix-ts) must satisfy
        now <= until <= now + MAX_FLAP_HOLD_SECS
    Out-of-range values are stale (past) or producer-bug (too far
    future); both cases unlink the file so the directory does not
    accumulate dead state.
    """
    if intentional_flap_dir is None:
        intentional_flap_dir = INTENTIONAL_FLAP_DIR
    if now_fn is None:
        now_fn = _time_mod.time
    path = os.path.join(intentional_flap_dir, switch, port)
    if not os.path.exists(path):
        return False
    kv = _parse_sentinel_kv(path)
    try:
        until = int(kv["until"])
    except (KeyError, ValueError):
        _unlink_quiet(path)
        return False
    now = int(now_fn())
    if until < now or until > now + MAX_FLAP_HOLD_SECS:
        _unlink_quiet(path)
        return False
    return True


def _forget_port_requested(port, forget_port_dir=None):
    """True iff a forget-port sentinel exists for `port`; consumes it (unlink).

    Flat, port-only keying (e.g. <dir>/et6b1) -- matches the bmc-fw-active claim
    precedent, since the triage rack is single-switch and the producer (the
    BMC-FW worker) knows only the internal port name. Presence is the request;
    we unlink before returning so it fires exactly once. Forgetting is
    idempotent, so a duplicate sentinel costs at most one extra wipe cycle.
    """
    if forget_port_dir is None:
        forget_port_dir = FORGET_PORT_DIR
    path = os.path.join(forget_port_dir, port)
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def _sol_session_active(bmc_ip, sol_active_dir=None):
    """True iff soltriage is holding an SOL session for bmc_ip.

    Reads <sol_active_dir>/<bmc_ip>; the file contains soltriage's PID.
    The sentinel is honoured only while the PID is still alive — a
    crashed soltriage that leaked its sentinel must not mask the BMC
    forever, so a stale (PID gone) file is unlinked and treated as
    inactive.
    """
    if not bmc_ip:
        return False
    if sol_active_dir is None:
        sol_active_dir = SOL_ACTIVE_DIR
    path = os.path.join(sol_active_dir, bmc_ip)
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except FileNotFoundError:
        return False
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return False


def _ensure_inband_admin_configured(
        port_state, bmc_ip, bmc_mac, kind, creds_used, emit_event,
        runner=None):
    """Run bmc-enable-inband-admin once per discovered BMC (per port).

    Triggered by port_worker_one_iter immediately after `bmcipmi` is
    set to a non-unknown value. The recipe is itself idempotent
    (Phosphor branch greps for `"15"` before patching; AMI is a no-op)
    but per-port sentinel suppresses repeat shellouts.

    The sentinel is keyed by (bmc_mac, kind) so:
      - a chassis swap (bmc_mac changes) re-fires the recipe,
      - a BMC kind flip (e.g. firmware update changing openbmc->traditional)
        re-fires too,
      - daemon restart re-fires once and then leaves alone.

    Failures emit an `inband_admin_setup_failed` event with the
    repr error; the sentinel is NOT set, so a later transition
    retries. Polling cycle never fails on this — the booted-host
    power-off path it unblocks is a downstream concern.

    `runner` is injectable for tests; production passes None and the
    helper shells out to BMC_ENABLE_INBAND_ADMIN_BIN.
    """
    if kind not in ("openbmc", "traditional"):
        return
    if not bmc_ip or not creds_used:
        return
    sentinel_key = (bmc_mac, kind)
    if port_state.get("inband_admin_configured_for") == sentinel_key:
        return
    user, password = creds_used
    if runner is None:
        if not os.path.exists(BMC_ENABLE_INBAND_ADMIN_BIN):
            return  # tool not deployed on this host yet
        def runner(ip, u, p):
            return subprocess.run(
                [BMC_ENABLE_INBAND_ADMIN_BIN, ip, u, p],
                check=True, capture_output=True, text=True,
                timeout=BMC_ENABLE_INBAND_ADMIN_TIMEOUT_SECS)
    try:
        runner(bmc_ip, user, password)
    except Exception as e:
        emit_event({
            "kind": "inband_admin_setup_failed",
            "switch": port_state["switch"],
            "port": port_state["port"],
            "bmc_ip": bmc_ip,
            "bmc_kind": kind,
            "error": repr(e),
        })
        return
    port_state["inband_admin_configured_for"] = sentinel_key
    emit_event({
        "kind": "inband_admin_configured",
        "switch": port_state["switch"],
        "port": port_state["port"],
        "bmc_ip": bmc_ip,
        "bmc_kind": kind,
    })


# ---------------------------------------------------------------------------
# 12-var per-port state machine
# ---------------------------------------------------------------------------

def port_worker_one_iter(port_state, switch_facts, emit_event, env):
    """Run one polling iteration of the 12-var state machine for one port.

    `switch_facts` is the latest snapshot from the switch fetcher: a dict
    keyed by (switch, port) whose values are fact dicts {linkstate, macs,
    lldp_neighbors, ...}. In the original switchportrecond the caller
    projected a per-port view {port: fact_dict} before calling this function;
    here we accept the full (switch, port)-keyed dict and look up our own key
    so callers can pass SwitchFactsCache directly.

    `env` is a SimpleNamespace carrying both probe callables and config paths.
    See module docstring for the full attribute list.

    Option (a) env routing: all probe callables are invoked via env.<name>()
    so tests can substitute lightweight stubs without touching any module-level
    globals.
    """
    port = port_state["port"]
    switch = port_state["switch"]

    # Resolve fact dict: accept either {(switch, port): fact} or {port: fact}
    # (the latter is what PortWorker in switchportrecond passed historically).
    fact = switch_facts.get((switch, port)) or switch_facts.get(port)
    if fact is None:
        # "Can't reach the switch" (its switch_facts row is reachable=false, so
        # it dropped out of the cache) is NON-INFORMATION, not a link change.
        # Hold the last-known linkstate and the inventory boundary untouched: a
        # transient switch-sense eAPI poll timeout marks the WHOLE switch
        # unreachable for one cycle, and forcing linkstate->unknown here would
        # bump linkstate.since and de-latch inventory on every port at once
        # (the 2026-06-21 incident). A genuine, sustained link loss is reported
        # by the switch as nolink (handled below) and debounced there.
        port_state["last_polled"] = _ts_now()
        return

    new_link = _link_value_from_eapi(fact["linkstate"])

    # Freeze per-port state when another actor (today: reaper-leased's
    # kick_via_switch_flap) has signalled an intentional flap on this
    # port. We are the cause; the chassis is still there; do not bump
    # linkstate.since (which would invalidate the inventory mtime check)
    # and do not run probes that will just time out while the port bounces.
    # Emit a forensic event only if linkstate WOULD have transitioned --
    # quiet during cycles that happen to catch the port still up.
    _flap_active = getattr(env, "intentional_flap_active", intentional_flap_active)
    if _flap_active(switch, port):
        if port_state["vars"]["linkstate"]["value"] != new_link:
            emit_event({
                "kind": "intentional_flap_observed",
                "switch": switch,
                "port": port,
                "raw_linkstate": fact["linkstate"],
                "would_have_been": new_link,
            })
        port_state["last_polled"] = _ts_now()
        return

    _set_var(port_state, "linkstate", new_link, emit_event)
    port_state["last_polled"] = _ts_now()

    if port_state["vars"]["linkstate"]["value"] != "link":
        # A switch-REPORTED non-link (nolink, or eAPI 'unknown'). Unlike the
        # cache miss above, the switch answered -- so linkstate display flips
        # immediately (honest). But a brief flap must NOT be mistaken for a
        # chassis swap, so the inventory boundary (link_session_since) and the
        # chassis-identity latches only break once the down has persisted
        # DOWN_DEBOUNCE_SECS. The BMC discovery path is short-circuited either
        # way -- a down port answers no probes.
        if port_state.get("link_down_since") is None:
            port_state["link_down_since"] = _ts_now()
        committed = _secs_since(port_state["link_down_since"]) >= \
            getattr(env, "down_debounce_secs", DOWN_DEBOUNCE_SECS)

        # Reachability vars are honestly unknown while the port is down.
        for v in ("bmcmac", "bmcip", "bmcping", "bmcipmi", "bmcpower",
                  "nodeip", "nodeping", "nodepxe", "nodessh", "multibmc"):
            _set_var(port_state, v, "unknown", emit_event)
        # Display "?" -- the previous watts are no longer trustworthy and
        # "0 W" would mislead operators into thinking the chassis is off
        # rather than gone.
        port_state["bmc_power"] = "?"
        port_state["bmcpower_unknown_streak"] = 0
        port_state["bmcpower_stale_since"] = None

        if committed:
            # Durable link-session break -> new session boundary. Forget the
            # whole identity (MACs + latches): the chassis may have been
            # physically swapped, so null the MAC/IP scalars, drop the BMC kind
            # cache and latched chassis serial, and de-latch inventory. They
            # re-acquire on link-up (and inventory_status returns notfound
            # against the advanced boundary until the node re-inventories).
            _forget_identity(port_state, emit_event)
        # else: sub-threshold flap -- HOLD chassissn + inventory at last-known.
        return

    # Link is up. A real flap may have just bumped the cosmetic linkstate.since;
    # the inventory boundary only advances on a committed down (above), so clear
    # the pending-down marker and seed the boundary on the first-ever link-up.
    port_state.pop("link_down_since", None)
    if port_state.get("link_session_since") is None:
        port_state["link_session_since"] = port_state["vars"]["linkstate"]["since"]

    # bmcmac: classify the per-port macs into BMC + NIC(s) + junk.
    # Honor the port's per-vid macmath (Task 4): the SONiC/Wedge mgmt mac on
    # a distinct_oui vid must resolve as the host nic_mac here, since that is
    # what flax_classify.feeder.derive_targets turns into the host
    # reservation. macmath_by_vid defaults to {} so any vid without a config
    # (and every existing test that builds env without it) gets macmath=None
    # -> legacy classification, unchanged.
    macs = fact["macs"]
    macmath_by_vid = getattr(env, "macmath_by_vid", {}) or {}
    port_macmath = None
    try:
        port_macmath = macmath_by_vid.get(int(fact.get("access_vid")))
    except (TypeError, ValueError):
        port_macmath = None
    classified = classify_macs(macs,
                               lldp_neighbors=fact.get("lldp_neighbors", []),
                               macmath=port_macmath)
    port_state["classification_source"] = classified.classification_source
    port_state["lldp_disagreement"] = classified.lldp_disagreement
    _persist_junk_macs(port_state, classified.junk, emit_event)

    leases_path = getattr(env, "leases_path", "/var/lib/misc/dnsmasq.leases")
    dhcp_hosts_dir = getattr(env, "dhcp_hosts_dir", "/etc/dnsmasq.dhcp-hosts")
    _lookup_lease_ip = getattr(env, "lookup_lease_ip", None)

    def _resolve_ip(mac):
        if _lookup_lease_ip is not None:
            return _lookup_lease_ip(leases_path, mac, dhcp_hosts_dir)
        from flax_observe.host_probe import lookup_lease_ip as _lip
        return _lip(leases_path, mac, dhcp_hosts_dir)

    # ------------------------------------------------------------------
    # Probe-driven role confirmation (flax_observe.role_confirm).
    #
    # MAC-ordering (classify_macs) chose CANDIDATES; the authoritative role
    # LABEL is confirmed here by what each visible MAC actually answers to.
    # A flip away from the heuristic requires a *positive* contradicting
    # signal (a BMC probe on another MAC, or a host login on the primary) --
    # never mere unreachability.
    #
    # Cost bound: a healthy primary (probes openbmc/traditional) is
    # confirmed with that single probe and NO further probing fires. Only an
    # anomalous primary (kind unknown/None) triggers host-probing it and
    # probing the other visible MACs.
    # ------------------------------------------------------------------
    from flax_observe.role_confirm import confirm_roles, CONFIRMED_BMC_KINDS

    visible = list(fact["macs"])
    access_vid = fact.get("access_vid")
    vlan_parents = getattr(env, "vlan_parents", None) or {}
    _ping6 = getattr(env, "ping6_reachable", ll_mod.ping6_reachable)
    ll_ping_timeout = getattr(env, "ll_ping_timeout", 2)
    _ping_host_fn = getattr(env, "ping_host", ping_host)

    def _ping4(ip):
        # Prefer an IPv4 reach only when the device actually answers there:
        # resolve_ip yields a *reservation* IP even for a device that hasn't
        # claimed it (a quiet OpenBMC switch pre-DHCP), and IPMI can't fall
        # back to LL. ICMP is the existing reachability signal (bmcping).
        return _ping_host_fn(ip) == "ok"

    credentials = getattr(env, "credentials", {})
    bmc_creds = getattr(env, "bmc_credentials", [])
    redfish_creds = getattr(env, "redfish_credentials", [])
    host_creds = getattr(env, "host_credentials", [])

    _probe_bmc_kind = getattr(env, "probe_bmc_kind", None)
    if _probe_bmc_kind is None:
        from flax_observe.bmc_probe import probe_bmc_kind as _probe_bmc_kind
    _ssh_uptime = getattr(env, "ssh_uptime", None)
    if _ssh_uptime is None:
        from flax_observe.host_probe import ssh_uptime as _ssh_uptime

    # Per-MAC probe caches so a MAC probed in the gather is reused (never
    # double-probed) by the downstream power/serial block.
    bmc_probe_by_mac = {}
    host_probe_by_mac = {}
    port_state["bmc_probe_by_mac"] = bmc_probe_by_mac
    port_state["host_probe_by_mac"] = host_probe_by_mac

    _prior_cache = port_state.get("bmc_kind_cached")

    def _bmc_kind_for(mac):
        # Reuse a fresh prior-cycle cache for this exact MAC so a healthy
        # confirmed BMC is probed ONCE (first cycle) then cached -- the gather
        # adds no per-cycle probe cost over the legacy downstream cache. A
        # cached "unknown" is NOT reused (transient: must re-probe so a
        # recovered BMC re-confirms).
        if (_prior_cache
                and _prior_cache.get("for_mac") == mac
                and _prior_cache.get("kind") in CONFIRMED_BMC_KINDS):
            probe = {"kind": _prior_cache.get("kind"),
                     "product_name": _prior_cache.get("product_name"),
                     "creds_used": _prior_cache.get("creds_used"),
                     "redfish_version": _prior_cache.get("redfish_version")}
            bmc_probe_by_mac[mac] = probe
            return probe["kind"]
        target, _is_ll = reach_for_mac(
            mac, access_vid, vlan_parents, _ping6, _resolve_ip,
            ll_ping_timeout, ping4=_ping4)
        if not target:
            probe = {"kind": "unknown", "product_name": None,
                     "creds_used": None}
        else:
            probe = _probe_bmc_kind(target, credentials, bmc_creds,
                                    redfish_creds=redfish_creds)
        bmc_probe_by_mac[mac] = probe
        return probe.get("kind")

    def _host_ok_for(mac):
        target, _is_ll = reach_for_mac(
            mac, access_vid, vlan_parents, _ping6, _resolve_ip,
            ll_ping_timeout, ping4=_ping4)
        if not target:
            res = "unknown"
        else:
            res = _ssh_uptime(target, host_creds)
        host_probe_by_mac[mac] = res
        return res

    primary = classified.bmc
    evidence = {}  # insertion order: primary first (confirm_roles relies on it)
    if primary:
        pk = _bmc_kind_for(primary)
        evidence[primary] = {"bmc_kind": pk, "host_ok": None}
        if pk not in CONFIRMED_BMC_KINDS:
            # Anomalous primary -> investigate. Host-probe the primary, then
            # the other visible MACs (bmc kind, and host login iff unknown).
            evidence[primary]["host_ok"] = _host_ok_for(primary)
            for m in visible:
                if m == primary:
                    continue
                mk = _bmc_kind_for(m)
                ev = {"bmc_kind": mk, "host_ok": None}
                if mk not in CONFIRMED_BMC_KINDS:
                    ev["host_ok"] = _host_ok_for(m)
                evidence[m] = ev

    verdict = confirm_roles(primary, list(classified.nics), evidence)

    port_state["bmc_mac"] = verdict.bmc_mac
    port_state["nic_mac"] = verdict.nic_mac
    port_state["nic_macs"] = list(classified.nics)
    port_state["role_source"] = verdict.source
    _set_var(port_state, "bmcmac",
             "found" if verdict.bmc_mac else "notfound", emit_event)
    _set_var(port_state, "multibmc",
             "found" if verdict.multi_bmc else "clear", emit_event)

    # Cache coherence: the final bmc_mac was just probed in the gather, so
    # prime bmc_kind_cached with its probe result. The downstream block's
    # needs_reprobe check then sees a fresh cache keyed to this MAC (and, for
    # a confirmed/promoted BMC, kind is openbmc/traditional) -> no re-probe.
    if verdict.bmc_mac and verdict.bmc_mac in bmc_probe_by_mac:
        _probe = bmc_probe_by_mac[verdict.bmc_mac]
        port_state["bmc_kind_cached"] = {
            "kind": _probe.get("kind"),
            "creds_used": _probe.get("creds_used"),
            "product_name": _probe.get("product_name"),
            "redfish_version": _probe.get("redfish_version"),
            "for_mac": verdict.bmc_mac,
        }

    # Preserve the ORIGINAL semantics of the downstream `mac_changed` check:
    # "this cycle's bmc_mac differs from the PRIOR cycle's cached for_mac".
    # The cache was just re-keyed to the current bmc_mac above, so the
    # downstream `cache.get('for_mac') != bmc_mac` would always be False and
    # silently kill chassis-swap handling. Compute the true value here from
    # _prior_cache (captured before the overwrite) and carry it downstream.
    role_mac_changed = (
        bool(_prior_cache) and _prior_cache.get("for_mac") != verdict.bmc_mac
    )

    if port_state["bmc_mac"]:
        bmcip = _resolve_ip(port_state["bmc_mac"])
        if bmcip:
            port_state["bmc_ip"] = bmcip
            _set_var(port_state, "bmcip", "found", emit_event)
        else:
            port_state["bmc_ip"] = None
            _set_var(port_state, "bmcip", "notfound", emit_event)
    else:
        # No confirmed BMC MAC -> no basis to vouch for any BMC IP. Clear it
        # (do NOT setdefault, which retained a stale lease): a node whose BMC
        # moved/vanished (Tioga Pass bmcmac=notfound after FW-update + lease
        # change) otherwise stranded a dead address the Triage tile offered SOL
        # + power against. Re-resolved fresh once the MAC is classifiable again.
        port_state["bmc_ip"] = None
        _set_var(port_state, "bmcip", "unknown", emit_event)

    # nic_ip is the probe target (lease-or-reservation). The nodeip VAR is
    # derived later by the boot pipeline ("DHCP'd this session"), decoupled
    # from mere IP assignment.
    if port_state.get("nic_mac"):
        port_state["nic_ip"] = _resolve_ip(port_state["nic_mac"]) or None
    else:
        port_state.setdefault("nic_ip", None)

    _ping_host = getattr(env, "ping_host", ping_host)
    _set_var(port_state, "bmcping", _ping_host(port_state.get("bmc_ip")), emit_event)
    _node_ping_raw = _ping_host(port_state.get("nic_ip"))

    # bmcipmi (kind) + bmcpower + chassissn
    bmc_ip      = port_state.get("bmc_ip")
    bmc_mac     = port_state.get("bmc_mac")
    credentials = getattr(env, "credentials", {})
    bmc_creds   = getattr(env, "bmc_credentials", [])
    redfish_creds = getattr(env, "redfish_credentials", [])

    # IPv6 link-local reach: as soon as the BMC mac is known AND the bang has
    # an IPv6 sub-interface on this port's access VLAN, we can ssh the OpenBMC
    # at fe80::EUI64%<iface> WITHOUT waiting for an IPv4 DHCP lease. This is the
    # *probe reach* only -- the reservation IP (bmc_ip) still comes from
    # classify/DHCP and coexists. Falls back to the IPv4-lease path when LL is
    # not reachable (no iface, no mac, or ping6 fails), preserving today's
    # behaviour exactly.
    #
    # env.vlan_parents maps vid (int) -> bang parent iface (str), loaded from
    # vlans.json by make_env (mirrors flax_reconcile's bmc_ll kick rung). The
    # sub-interface is "<parent>.<vid>".
    vlan_parents = getattr(env, "vlan_parents", None) or {}
    access_vid = fact.get("access_vid")
    _ping6 = getattr(env, "ping6_reachable", ll_mod.ping6_reachable)
    ll_ping_timeout = getattr(env, "ll_ping_timeout", 2)
    # reach_for_mac falls back to resolve_ip(bmc_mac) which, for the chosen
    # bmc_mac, equals bmc_ip (computed above from the same _resolve_ip). For a
    # falsy bmc_mac it returns None -- matching the old `bmc_ll or bmc_ip` when
    # bmc_ip was also None. Behaviour is identical to the prior inline block.
    target, is_ll = reach_for_mac(
        bmc_mac, access_vid, vlan_parents, _ping6, _resolve_ip,
        ll_ping_timeout, ping4=_ping4)
    bmc_ll = target if is_ll else None
    # Always record for visibility (None when LL is not the reach path).
    port_state["bmc_ll"] = bmc_ll

    # The host the OpenBMC ssh probe targets: prefer the IPv6 link-local (no
    # DHCP wait) and fall back to the IPv4 lease. IPMI-over-LAN (traditional
    # BMCs / UDP:623 power+serial) stays on bmc_ip below.
    probe_host = target

    _probe_bmc_kind = getattr(env, "probe_bmc_kind", None)
    _bmc_power_status_openbmc = getattr(env, "bmc_power_status_openbmc", None)
    _bmc_power_and_sdr_traditional = getattr(env, "bmc_power_and_sdr_traditional", None)
    _chassis_serial_traditional = getattr(env, "chassis_serial_traditional", None)
    _chassis_serial_openbmc = getattr(env, "chassis_serial_openbmc", None)
    _sol_active = getattr(env, "sol_session_active", _sol_session_active)
    _inband_admin = getattr(env, "ensure_inband_admin_configured",
                            _ensure_inband_admin_configured)

    if _probe_bmc_kind is None:
        from flax_observe.bmc_probe import probe_bmc_kind as _probe_bmc_kind
    if _bmc_power_status_openbmc is None:
        from flax_observe.bmc_probe import bmc_power_status_openbmc as _bmc_power_status_openbmc
    if _bmc_power_and_sdr_traditional is None:
        from flax_observe.bmc_probe import bmc_power_and_sdr_traditional as _bmc_power_and_sdr_traditional
    if _chassis_serial_traditional is None:
        from flax_observe.bmc_probe import chassis_serial_traditional as _chassis_serial_traditional
    if _chassis_serial_openbmc is None:
        from flax_observe.bmc_probe import chassis_serial_openbmc as _chassis_serial_openbmc

    if not probe_host:
        port_state.pop("bmc_kind_cached", None)
        # Cleared (not latched like chassis_sn) when the BMC IP is unknown:
        # see the no-latch rationale where product_name is surfaced below.
        port_state["product_name"] = None
        # Display "?" -- without a BMC IP the previous watts are not a
        # trustworthy read of current chassis state.
        port_state["bmc_power"] = "?"
        port_state["bmcpower_stale_since"] = None
        _set_var(port_state, "bmcipmi",   "unknown", emit_event)
        _set_var(port_state, "bmcpower",  "unknown", emit_event)
        _set_var(port_state, "chassissn", "unknown", emit_event)
    else:
        cache = port_state.get("bmc_kind_cached")
        # Computed in the role-confirmation block above against the PRIOR
        # cycle's cache (the cache here was already re-keyed to this cycle's
        # bmc_mac, so a local compare would always read False).
        mac_changed = role_mac_changed
        # Re-probe when:
        #   - never probed (cache missing)
        #   - bmc_mac changed (different chassis on the same port)
        #   - last probe returned "unknown" (transient BMC unresponsiveness:
        #     do not latch a negative result forever -- that locks the port
        #     into perpetual notfound even after the BMC recovers).
        needs_reprobe = (
            cache is None
            or mac_changed
            or cache.get("kind") == "unknown"
        )
        # Cache coherence with the role-confirmation gather above: if THIS
        # bmc_mac was already probed in this very cycle (its probe result is in
        # bmc_probe_by_mac and the primed cache is keyed to it), do NOT probe a
        # second time -- the gather's result is fresh. This preserves the cost
        # bound (one bmc-kind probe per MAC per cycle) even for an unknown-kind
        # primary that the heuristic kept as the BMC.
        if (needs_reprobe and not mac_changed
                and bmc_mac in bmc_probe_by_mac
                and bool(cache) and cache.get("for_mac") == bmc_mac):
            needs_reprobe = False
        if needs_reprobe:
            probe = _probe_bmc_kind(probe_host, credentials, bmc_creds,
                                    redfish_creds=redfish_creds)
            cache = {"kind": probe["kind"],
                     "creds_used": probe["creds_used"],
                     "product_name": probe.get("product_name"),
                     "redfish_version": probe.get("redfish_version"),
                     "for_mac": bmc_mac}
            port_state["bmc_kind_cached"] = cache
            port_state["bmc_power"] = "0 W"
            if mac_changed:
                # Different physical chassis on this port. Clear the latched
                # serial so we re-acquire it for the new chassis instead of
                # carrying the old one over.
                port_state["chassis_sn"] = None
                port_state["bmcpower_unknown_streak"] = 0
                port_state["bmcpower_stale_since"] = None

        kind = cache["kind"]
        creds_used = cache["creds_used"]
        # product_name is re-read from the probe cache every cycle -- NO latch,
        # unlike chassis_sn (which is held through transient probe failures at
        # the chassissn block below). Immutable per chassis, but cheap to
        # re-acquire; the flax-discover consumer latches the derived family and
        # preserves an already-latched product_name when a cycle reports None,
        # so transient flicker here is tolerated by design.
        port_state["product_name"] = cache.get("product_name")
        _set_var(port_state, "bmcipmi", kind, emit_event)
        # New BMC classification -> grant in-band admin priv (KCS) so the
        # booted host's `ipmitool chassis power off` works. Idempotent;
        # sentinel skips re-runs once configured.
        # OpenBMC in-band-admin + power/serial are ssh-based: reach them via
        # the same probe_host (LL when up, else the IPv4 lease).
        _inband_admin(port_state, probe_host, bmc_mac, kind, creds_used,
                      emit_event)

        prev_streak = port_state.get("bmcpower_unknown_streak", 0)
        sn = None
        pwr = "unknown"
        watts = None
        if kind == "openbmc" and creds_used:
            pwr = _bmc_power_status_openbmc(probe_host, creds_used)
            sn = _chassis_serial_openbmc(probe_host, creds_used)
            # Some openbmc BMCs (e.g. Tioga Pass) do not expose chassis
            # or product serial via the SSH FRU path that
            # chassis_serial_openbmc uses, but DO expose them via
            # traditional IPMI on UDP:623. Walk bmc_creds for a working
            # pair before giving up.
            if sn is None:
                for c in bmc_creds:
                    sn = _chassis_serial_traditional(
                        bmc_ip, (c["bmcuser"], c["bmcpass"]))
                    if sn is not None:
                        break
        elif kind == "traditional" and creds_used:
            # Mitigation 1: skip if soltriage holds an SOL session on this
            # BMC -- competing RMCP+ sessions evict the SOL slot on AMI
            # MegaRAC's small (4-8 slot) session table.
            if _sol_active(bmc_ip):
                emit_event({
                    "kind": "sol_active_skip",
                    "switch": switch,
                    "port": port,
                    "bmc_ip": bmc_ip,
                })
                # leave bmcpower + chassissn latched at last value
                pwr = port_state["vars"]["bmcpower"]["value"]
                sn = None
            else:
                # Mitigation 3: one RMCP+ session for both `power status`
                # and `sdr` via ipmitool's `exec` script form.
                pwr, watts = _bmc_power_and_sdr_traditional(bmc_ip, creds_used)
                # Mitigation 2: Product Serial is invariant for a given
                # chassis. Once latched, skip the refetch every cycle --
                # saves one RMCP+ session per poll. Hardware swap clears
                # chassis_sn above (mac_changed branch), forcing refetch.
                if port_state.get("chassis_sn") and not mac_changed:
                    sn = None  # latch keeps the existing value below
                else:
                    sn = _chassis_serial_traditional(bmc_ip, creds_used)

        # Latch decision: while the BMC is identified (chassis_sn latched)
        # and still pingable (bmcping=ok), a single failed IPMI power poll
        # should not flip bmcpower to unknown -- it is almost always a
        # transient session-table or RMCP+ glitch on the BMC. Hold the
        # previous on/off value until the wider context (bmcping, mac,
        # chassis_sn, link) actually breaks.
        #
        # BUT the hold is BOUNDED: after LATCH_MAX_UNKNOWN_STREAK consecutive
        # unknown polls, give up and let the real (unknown) value through.
        # Otherwise a node that powers OFF but whose BMC then stops answering
        # IPMI (AMI session-table exhaustion, common right after a power-down)
        # held a confidently-wrong "on"/watts on the Triage tile indefinitely.
        # Past the bound the tile shows '—' (unreadable), never a wrong value.
        bmcping_value = port_state["vars"]["bmcping"]["value"]
        prev_pwr = port_state["vars"]["bmcpower"]["value"]
        chassis_sn_latched = bool(port_state.get("chassis_sn"))
        latch_eligible = (
            pwr == "unknown"
            and bmcping_value == "ok"
            and chassis_sn_latched
            and prev_pwr in ("on", "off")
            and prev_streak < LATCH_MAX_UNKNOWN_STREAK
        )

        if latch_eligible:
            # Hold previous bmcpower; record when the last fresh poll was.
            if port_state.get("bmcpower_stale_since") is None:
                port_state["bmcpower_stale_since"] = port_state.get("last_polled")
            # Do NOT update bmc_power watts (stays at last successful value).
            # Streak still ticks so observability is preserved.
            port_state["bmcpower_unknown_streak"] = prev_streak + 1
            if port_state["bmcpower_unknown_streak"] == 3:
                emit_event({
                    "kind": "bmc_poll_failed",
                    "switch": switch,
                    "port": port,
                    "bmc_ip": bmc_ip,
                    "bmc_mac": bmc_mac,
                    "consecutive_unknowns": 3,
                    "latched": True,
                })
        else:
            _set_var(port_state, "bmcpower", pwr, emit_event)
            # Node power-on: a confirmed off->on means the host will re-netboot
            # and re-inventory, so start a new inventory session -- inventory
            # goes notfound now and the operator watches it return to found as
            # the post lands. Gated to off->on ONLY: never unknown->on. bmcpower
            # is deliberately NOT hydrated on restart (PortWorker._hydrate resets
            # it to unknown), so the first post-restart cycle is unknown->on and
            # cannot manufacture a spurious reset for a node that booted during
            # the downtime. The bmcpower latch above already absorbs transient
            # unknown reads, so an off->on reaching here is a real, live-observed
            # boot. Chassis identity is preserved (same chassis) -- hence the
            # lighter _advance_inventory_boundary, not _forget_identity.
            if prev_pwr == "off" and pwr == "on":
                _advance_inventory_boundary(port_state)
                emit_event({
                    "kind": "inventory_reset_on_power_on",
                    "switch": switch,
                    "port": port,
                    "bmc_mac": bmc_mac,
                })
            # bmc_power carries the real HSC input-power reading regardless of
            # on/off: a powered-off host still draws standby watts, and the UI
            # shows that draw while the bmcpower var (set above) conveys the
            # on/off status. "?" when no watts could be parsed this cycle
            # (on-but-no-sensor, or an unknown/unreachable poll) -- never a
            # stale leftover from a previous power moment.
            port_state["bmc_power"] = watts if watts else "?"
            port_state["bmcpower_stale_since"] = None

            if pwr == "unknown":
                port_state["bmcpower_unknown_streak"] = prev_streak + 1
                if port_state["bmcpower_unknown_streak"] == 3:
                    emit_event({
                        "kind": "bmc_poll_failed",
                        "switch": switch,
                        "port": port,
                        "bmc_ip": bmc_ip,
                        "bmc_mac": bmc_mac,
                        "consecutive_unknowns": 3,
                        "latched": False,
                    })
            else:
                if prev_streak >= 3:
                    emit_event({
                        "kind": "bmc_poll_recovered",
                        "switch": switch,
                        "port": port,
                        "bmc_ip": bmc_ip,
                        "bmc_mac": bmc_mac,
                        "streak_was": prev_streak,
                    })
                port_state["bmcpower_unknown_streak"] = 0

        if sn:
            port_state["chassis_sn"] = sn
            _set_var(port_state, "chassissn", "found", emit_event)
        elif port_state.get("chassis_sn"):
            # Latch: Product Serial is an immutable property of the
            # physical chassis. Once we have read it successfully, transient
            # IPMI/SSH failures should not clobber it back to notfound.
            # The latch is cleared on link drop (chassis swap signal)
            # and on bmc_mac change (different chassis on same port).
            _set_var(port_state, "chassissn", "found", emit_event)
        else:
            port_state["chassis_sn"] = None
            _set_var(port_state, "chassissn", "notfound", emit_event)

    # --- node-side vars: each stands on its OWN signal. NO monotonic
    # cross-gating -- gating inventory behind the live node chain un-latched
    # inventory for idle/off nodes (0.9.37/0.9.38 regression). nodeip and
    # nodepxe are session-relative (reset when a power-on advances the boundary);
    # nodeping/nodessh are live; inventory is latched via inventory_status
    # (mtime vs boundary), independent of whether the node is currently up.
    if port_state.get("nic_mac"):
        _boundary = (port_state.get("link_session_since")
                     or port_state["vars"]["linkstate"]["since"])

        # nodeip = "DHCP'd this session" (fresh kea.lease4 cltt >= boundary),
        # NOT merely "an IP is assigned" -- so a power-on (boundary advance)
        # resets it to notfound until the node re-requests.
        _kea_lease_fresh = getattr(env, "kea_lease_fresh", None)
        _set_var(port_state, "nodeip",
                 "found" if (_kea_lease_fresh
                             and _kea_lease_fresh(port_state["nic_mac"], _boundary))
                 else "notfound",
                 emit_event)

        _set_var(port_state, "nodeping", _node_ping_raw, emit_event)

        _nginx_pxe_seen = getattr(env, "nginx_pxe_seen", None)
        if _nginx_pxe_seen is None:
            from flax_observe.host_probe import nginx_pxe_seen as _nginx_pxe_seen
        _set_var(port_state, "nodepxe", _nginx_pxe_seen(
            getattr(env, "nginx_access_log", "/var/log/nginx/access.log"),
            port_state.get("nic_ip"), _boundary), emit_event)

        _ssh_uptime = getattr(env, "ssh_uptime", None)
        if _ssh_uptime is None:
            from flax_observe.host_probe import ssh_uptime as _ssh_uptime
        _set_var(port_state, "nodessh", _ssh_uptime(
            port_state.get("nic_ip"),
            getattr(env, "host_credentials", [])), emit_event)

        # inventory stays its own latched fact ("post collected this session"),
        # never forced by the upstream stages -- a provisioned node keeps
        # inventory=found through idle/power-off.
        _inventory_status = getattr(env, "inventory_status", None)
        if _inventory_status is None:
            from flax_observe.host_probe import inventory_status as _inventory_status
        _set_var(port_state, "inventory", _inventory_status(
            getattr(env, "nodes_root", "/export/nodes"),
            port_state["nic_mac"], _boundary), emit_event)
    else:
        # No nic_mac: nothing to probe; all node-side stages unknown.
        for _stage in NODE_PIPELINE:
            _set_var(port_state, _stage, "unknown", emit_event)
