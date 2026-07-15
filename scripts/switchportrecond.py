#!/usr/bin/env python3
"""switchportrecond — per-site switch port recon daemon.

See docs/superpowers/specs/2026-05-06-switchportrecon-consolidation-design.md.

This file is a single-file Python service (stdlib only). Logical
sections appear in this order:
  1. Constants + config loading
  2. Logging + events writer (RotatingFileHandler-driven)
  3. State model (port_state, locks, snapshot debouncer)
  4. Switch drivers (EosDriver; Cumulus + IOS stubs)
  5. Switch fetcher loop
  6. Port worker loop (12-var state machine)
  7. HTTP surface (JSON API + HTML dashboard + drill-down)
  8. Main (--check-vip-holder, signal handling, threading boot)
"""

# === 1. Constants + config loading ===========================================

import argparse
import sys

SCHEMA_VERSION = 1
STATE_ROOT = "/opt/flax/var/switchportrecon"
SNAPSHOT_PATH = STATE_ROOT + "/state.json"
EVENTS_PATH = STATE_ROOT + "/events.jsonl"
# Shared SSH known_hosts file for both reaper-leased and switchportrecond.
# Replaces UserKnownHostsFile=/dev/null — keeps key add-warnings to first
# connection only instead of every invocation.
SSH_KNOWN_HOSTS = "/opt/flax/var/ssh/known_hosts"
# Operator-installed via roles/apply_bmc_scripts (see that role's docstring).
# Auto-fired by this daemon when a BMC is first classified — see
# _ensure_inband_admin_configured.
BMC_ENABLE_INBAND_ADMIN_BIN = "/opt/flax/bin/bmc-enable-inband-admin"
BMC_ENABLE_INBAND_ADMIN_TIMEOUT_SECS = 30
GEOMETRY_PATH = "/etc/flax/geometry.json"
SWITCHES_PATH = "/etc/flax/switches.json"
CREDENTIALS_PATH = "/etc/flax/credentials.json"
BMC_CREDENTIALS_PATH = "/etc/flax/credentials-bmc.json"
HOST_CREDENTIALS_PATH = "/etc/flax/credentials-host.json"
REDFISH_CREDENTIALS_PATH = "/etc/flax/credentials-redfish.json"

DASHBOARD_PORT = 10988

CYCLE_SECS = 10.0
SNAPSHOT_MIN_INTERVAL_SECS = 1.0
EVENTS_MAX_BYTES = 10 * 1024 * 1024
EVENTS_BACKUP_COUNT = 4

IPMITOOL_TIMEOUT_SECS = 15
SSH_TIMEOUT_SECS = 8
EAPI_TIMEOUT_SECS = 5
PING_PACKETS = 1
PING_WAIT_SECS = 1

# soltriage drops /run/flax/sol-active/<bmc_ip> containing its PID while
# it holds an SOL session; port_worker_one_iter skips the traditional-
# branch IPMI probes for that BMC to avoid evicting the SOL session from
# the BMC's tiny RMCP+ session table (AMI MegaRAC: 4–8 slots).
SOL_ACTIVE_DIR = "/run/flax/sol-active"

# `bmc-reset-via-redfish` recovery binary + rate-limit. _default_ipmi_runner
# auto-fires this when an AMI BMC returns "insufficient resources for
# session" — clears the session table. ~3 min BMC outage, so cap to once
# per BMC per 60 min and run fire-and-forget (Popen, no wait).
REDFISH_RESET_BIN = "/opt/flax/bin/bmc-reset-via-redfish"
REDFISH_RESET_RATE_LIMIT_SECS = 3600

# reaper-leased (or any other actor that intentionally bounces a switch
# port) drops a sentinel under <dir>/<switch>/<port> containing key=value
# lines (`until=<unix_ts>`, `reason=<token>`, optional `mac=`, `pid=`)
# BEFORE issuing the flap. While the sentinel is fresh, port_worker_one_iter
# freezes per-port state: no linkstate transition recorded, no var reset,
# no IPMI/ssh probe — the link is about to come back. MAX_FLAP_HOLD_SECS
# is the hard ceiling on `until`: a producer that writes farther in the
# future (clock skew or bug) is rejected so it can't mute a port forever.
INTENTIONAL_FLAP_DIR = "/run/flax/intentional-flap"
MAX_FLAP_HOLD_SECS = 120

# LLDP cache for inter-daemon coordination. switchportrecond writes
# this file once per fetch cycle; reaper-leased reads it to decide
# whether to reject a junk MAC during enrollment. File-format-only
# contract — same pattern as INTENTIONAL_FLAP_DIR — so constants are
# duplicated in reaper_leased.py. Do not refactor into a shared
# module without revisiting the daemon isolation model.
LLDP_CACHE_PATH = "/run/flax/lldp-by-port.json"
LLDP_CACHE_MAX_AGE_SECS = 300  # readers reject older snapshots

# Updated by SwitchFetcher under _facts_lock; read by main() to drive
# write_lldp_cache. Keyed by switch name -> {port: [neighbor_dict]}.
_LLDP_BY_SWITCH = {}

STATE_VARS = [
    "linkstate", "bmcmac", "bmcip", "bmcping", "bmcipmi",
    "bmcpower", "chassissn", "nodeip", "nodeping", "nodepxe",
    "nodessh", "inventory",
]


class ConfigError(Exception):
    pass


def load_credentials(path):
    """Load /etc/flax/credentials.json; reject vault-encrypted files."""
    with open(path) as f:
        first = f.readline()
        if first.startswith("$ANSIBLE_VAULT"):
            raise ConfigError(
                f"{path} is ansible-vault encrypted; decrypt before deploy"
            )
        rest = f.read()
    return json.loads(first + rest)


def load_bmc_credentials(path):
    """Load /etc/flax/credentials-bmc.json — list of {bmcuser, bmcpass}.

    Reject ansible-vault encrypted files. Empty list is allowed (host
    with no traditional-IPMI BMCs).
    """
    with open(path) as f:
        first = f.readline()
        if first.startswith("$ANSIBLE_VAULT"):
            raise ConfigError(
                f"{path} is ansible-vault encrypted; decrypt before deploy"
            )
        rest = f.read()
    data = json.loads(first + rest)
    if not isinstance(data, list):
        raise ConfigError(f"{path}: expected list of {{bmcuser, bmcpass}}")
    for c in data:
        if "bmcuser" not in c or "bmcpass" not in c:
            raise ConfigError(f"{path}: entry missing bmcuser/bmcpass: {c!r}")
    return data


def load_host_credentials(path):
    """Load /etc/flax/credentials-host.json — list of {user, pass}.

    Same shape as reaper-leased reads; ssh_uptime walks this list to
    probe a node's ssh reachability. Reject ansible-vault encrypted
    files. Empty list is allowed (no nodes with shell access expected).
    """
    with open(path) as f:
        first = f.readline()
        if first.startswith("$ANSIBLE_VAULT"):
            raise ConfigError(
                f"{path} is ansible-vault encrypted; decrypt before deploy"
            )
        rest = f.read()
    data = json.loads(first + rest)
    if not isinstance(data, list):
        raise ConfigError(f"{path}: expected list of {{user, pass}}")
    for c in data:
        if "user" not in c or "pass" not in c:
            raise ConfigError(f"{path}: entry missing user/pass: {c!r}")
    return data


def load_switches(path):
    """Load /etc/flax/switches.json — list of {name, driver, host, credentials_key}."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ConfigError(f"{path}: expected non-empty list")
    for s in data:
        for k in ("name", "driver", "host", "credentials_key"):
            if k not in s:
                raise ConfigError(f"{path}: switch entry missing '{k}': {s!r}")
    return data


def load_geometry(path, default_switch_name=None):
    """Load /etc/flax/geometry.json — flat list of {port, ou, switch?}.

    If 'switch' is omitted on an entry, default_switch_name is filled in.
    """
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ConfigError(f"{path}: expected list")
    out = []
    for e in data:
        if "port" not in e or "ou" not in e:
            raise ConfigError(f"{path}: entry missing 'port' or 'ou': {e!r}")
        switch = e.get("switch") or default_switch_name
        if not switch:
            raise ConfigError(
                f"{path}: entry {e!r} has no 'switch' and no default given"
            )
        out.append({"port": e["port"], "ou": e["ou"], "switch": switch})
    return out


def _normalize_mac(s):
    """Return lowercase colon-separated MAC, accepting :, -, or . separators."""
    digits = "".join(c for c in s.lower() if c in "0123456789abcdef")
    if len(digits) != 12:
        raise ValueError(f"bad MAC string: {s!r}")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _mac_to_int(mac):
    return int(mac.replace(":", ""), 16)


def _int_to_mac(n):
    h = format(n & 0xFFFFFFFFFFFF, "012x")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


import collections as _collections


ClassifiedMacs = _collections.namedtuple(
    "ClassifiedMacs",
    ["bmc", "nics", "junk", "classification_source", "lldp_disagreement"],
)


def _paired_nic_offsets():
    """Stride for paired NIC pattern. BMC - 2k for k in 1..4 covers
    leopard/tiogapass (k=1) and the multi-blade sameport families
    (brycecanyon and similar; k=1..4)."""
    return [2, 4, 6, 8]


def _is_paired_nic_of(nic, bmc):
    """True iff nic ∈ {bmc-2, bmc-4, bmc-6, bmc-8} (paired-NIC pattern)."""
    try:
        diff = _mac_to_int(bmc) - _mac_to_int(nic)
    except ValueError:
        return False
    return diff in _paired_nic_offsets()


def _highest_paired_partner(lldp_mac, observed):
    """If lldp_mac is the NIC-side of a paired-NIC chassis (BMC = NIC +
    2/4/6/8), return the highest observed MAC that fits. Otherwise None.
    Handles the leopard6b4 pattern where the host OS LLDPs from its data
    NIC but the BMC RJ45 stays silent on the BMC switch.
    """
    base = _mac_to_int(lldp_mac)
    partners = [_int_to_mac(base + off) for off in _paired_nic_offsets()]
    matches = [p for p in partners if p in observed]
    return max(matches, key=_mac_to_int) if matches else None


def classify_macs(macs, lldp_neighbors=None):
    """Identify the legitimate BMC on a switch port, the paired NICs,
    and any junk MACs that don't fit either pattern.

    Returns a ClassifiedMacs namedtuple. Decision tree:

    - Empty macs → ClassifiedMacs(None, [], [], "single_mac", False).
    - One mac → BMC = that mac, NIC synthesized at BMC-2 (back-compat
      with paired-host families when only the BMC has DHCP'd so far).
    - LLDP shows exactly one neighbor whose MAC is in macs → that's
      the BMC. Source = "lldp". Other macs are paired NICs (match the
      bmc-2k pattern) or junk.
    - LLDP shows multiple neighbors all matching macs → lldp_disagreement
      = True. Fall back to numeric-highest BMC, with the OTHER LLDP-
      claimed MACs becoming junk (reason multi_lldp_bmc_observed).
    - LLDP shows no matching neighbors → numeric fallback. BMC = highest
      MAC. Non-paired macs become junk (reason
      not_paired_with_numeric_bmc).
    """
    if lldp_neighbors is None:
        lldp_neighbors = []
    if not macs:
        return ClassifiedMacs(None, [], [], "single_mac", False)

    norm = [_normalize_mac(m) for m in macs]
    if len(norm) == 1:
        bmc = norm[0]
        nic = _int_to_mac(_mac_to_int(bmc) - 2)
        return ClassifiedMacs(bmc, [nic], [], "single_mac", False)

    lldp_macs_on_port = []
    for n in lldp_neighbors:
        try:
            m = _normalize_mac(n.get("mac", ""))
        except ValueError:
            continue
        if m in norm:
            lldp_macs_on_port.append(m)

    if len(lldp_macs_on_port) == 1:
        lldp_mac = lldp_macs_on_port[0]
        # Invert when the LLDP-matched MAC is the NIC of a paired-NIC
        # chassis whose BMC is silent on the BMC switch (leopard6b4 /
        # tiogapass8b1-2-4 pattern, observed 2026-05-24). The highest
        # +2/+4/+6/+8 partner is the actual BMC.
        higher_partner = _highest_paired_partner(lldp_mac, norm)
        if higher_partner is not None:
            bmc = higher_partner
            nics = [lldp_mac]
            junk = []
            for m in norm:
                if m in (bmc, lldp_mac):
                    continue
                if _is_paired_nic_of(m, bmc):
                    nics.append(m)
                else:
                    junk.append({"mac": m,
                                 "reason": "not_paired_with_lldp_bmc"})
            return ClassifiedMacs(bmc, nics, junk,
                                  "lldp_nic_inverted", False)

        bmc = lldp_mac
        nics = []
        junk = []
        for m in norm:
            if m == bmc:
                continue
            if _is_paired_nic_of(m, bmc):
                nics.append(m)
            else:
                junk.append({"mac": m,
                             "reason": "not_paired_with_lldp_bmc"})
        return ClassifiedMacs(bmc, nics, junk, "lldp", False)

    if len(lldp_macs_on_port) >= 2:
        bmc = max(norm, key=_mac_to_int)
        nics = []
        junk = []
        for m in norm:
            if m == bmc:
                continue
            if m in lldp_macs_on_port:
                junk.append({"mac": m,
                             "reason": "multi_lldp_bmc_observed"})
            elif _is_paired_nic_of(m, bmc):
                nics.append(m)
            else:
                junk.append({"mac": m,
                             "reason": "not_paired_with_numeric_bmc"})
        return ClassifiedMacs(bmc, nics, junk, "numeric_fallback", True)

    # No LLDP match → numeric fallback.
    bmc = max(norm, key=_mac_to_int)
    nics = []
    junk = []
    for m in norm:
        if m == bmc:
            continue
        if _is_paired_nic_of(m, bmc):
            nics.append(m)
        else:
            junk.append({"mac": m, "reason": "not_paired_with_numeric_bmc"})
    return ClassifiedMacs(bmc, nics, junk, "numeric_fallback", False)

# === 2. Logging + events writer =============================================

import datetime
import json
import logging
from logging.handlers import RotatingFileHandler


def _ts_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


class EventsWriter:
    """Single-thread JSONL events writer using RotatingFileHandler.

    Each emit() call adds an automatic 'ts' field if absent and writes
    a newline-terminated JSON record to events.jsonl. The file rotates
    at max_bytes; up to backup_count rotated files are kept.
    """

    def __init__(self, log_path, max_bytes=EVENTS_MAX_BYTES,
                 backup_count=EVENTS_BACKUP_COUNT):
        self._handler = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup_count
        )
        # We're writing JSONL ourselves; the formatter is just message
        # passthrough. Newline is added by Logger.makeRecord/emit.
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger("switchportrecond.events")
        self._logger.setLevel(logging.INFO)
        # Avoid double-emission via the root logger.
        self._logger.propagate = False
        # Idempotent: don't pile up handlers across daemon-reload.
        for h in list(self._logger.handlers):
            self._logger.removeHandler(h)
        self._logger.addHandler(self._handler)

    def emit(self, event: dict):
        if "ts" not in event:
            event = {"ts": _ts_now(), **event}
        self._logger.info(json.dumps(event, separators=(",", ":")))

    def flush_and_close(self):
        self._handler.flush()
        self._handler.close()
        self._logger.removeHandler(self._handler)

# === 3. State model =========================================================

import re
import threading


def display_port(port):
    """et6b1 → Et6/1 (the format the bash exposes in status.json)."""
    m = re.match(r"^et(\d+)b(\d+)$", port)
    if not m:
        return port  # passthrough for non-Arista breakout names
    return f"Et{m.group(1)}/{m.group(2)}"


def new_port_state(switch, port, ou, index):
    """Initialize a PortState dict with every var = unknown."""
    return {
        "switch": switch,
        "port": port,
        "ou": ou,
        "index": index,
        "vars": {v: {"value": "unknown", "since": None} for v in STATE_VARS},
        # Per-var resolved data (filled by port worker as discovery progresses):
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
    }


def render_status_snapshot(ps, deepest_state):
    """PortState → back-compat status.json shape consumed by the api container.

    deepest_state is the state-machine variable that's the 'deepest reached'
    in this poll cycle (e.g. 'bmcmac' once the bmc MAC is known but bmcip
    isn't yet); see Section C.1 of the spec.

    `time` is the transition timestamp of the currently-deepest var (i.e.
    when this row entered its current state) — NOT the last poll time —
    so the UI's relative-time renderer doesn't tick on every cycle. The
    poll-freshness signal lives in `polled_at` for the daemon's own
    dashboard; consumers that need both should read both.
    """
    ou = re.match(r"^(\d+)([LCR])$", ps["ou"])
    ou_num = ou.group(1) if ou else ps["ou"]
    column = ou.group(2) if ou else ""
    state = deepest_state or "linkstate"
    state_since = ps["vars"].get(state, {}).get("since")
    polled_at = ps["last_polled"] or _ts_now()
    return {
        "serviceindex": str(ps["index"]),
        "port": display_port(ps["port"]),
        "ou": ou_num,
        "column": column,
        "state": state,
        "time": state_since or polled_at,
        "polled_at": polled_at,
        "chassis": ps.get("chassis_sn") or "",
        "mac": ps.get("bmc_mac") or "",
        "ip": ps.get("bmc_ip") or "",
        "nodemac": ps.get("nic_mac") or "",
        "nodeip": ps.get("nic_ip") or "",
        "power": ps.get("bmc_power") or "0 W",
        "nic_macs": ps.get("nic_macs", []),
        "junk_macs": list(ps.get("junk_macs", [])),
        "classification_source": ps.get("classification_source"),
        "lldp_disagreement": ps.get("lldp_disagreement", False),
        "bmcpower_stale_since": ps.get("bmcpower_stale_since"),
    }


def write_snapshot(path, port_state):
    """Atomic write: tmp + fsync + rename."""
    tmp = path + ".tmp"
    payload = {
        "version": SCHEMA_VERSION,
        "snapshotted_at": _ts_now(),
        "ports": {
            f"{sw}:{p}": v for (sw, p), v in port_state.items()
        },
    }
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def load_snapshot(path):
    """Read state.json into a {(switch, port): PortState} dict.

    Missing file → empty dict (fresh start).
    """
    import os
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        payload = json.load(f)
    out = {}
    for key, ps in payload.get("ports", {}).items():
        sw, _, p = key.partition(":")
        out[(sw, p)] = ps
    return out


class SnapshotDebouncer:
    """Background thread; writes state.json at most once per min_interval_secs.

    mark_dirty() is non-blocking — sets an event the worker wakes on.
    """

    def __init__(self, path, port_state, state_lock,
                 min_interval_secs=SNAPSHOT_MIN_INTERVAL_SECS,
                 events_writer=None):
        self._path = path
        self._port_state = port_state
        self._state_lock = state_lock
        self._min_interval = min_interval_secs
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._events = events_writer
        self._thread = threading.Thread(
            target=self._run, name="snapshot-debouncer", daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._dirty.set()  # wake the worker so it can exit
        self._thread.join(timeout=5.0)

    def mark_dirty(self):
        self._dirty.set()

    def _run(self):
        while not self._stop.is_set():
            self._dirty.wait()
            if self._stop.is_set():
                return
            self._dirty.clear()
            # Sleep the debounce window to coalesce bursts
            _time = __import__("time")
            _time.sleep(self._min_interval)
            try:
                with self._state_lock:
                    snapshot = dict(self._port_state)
                write_snapshot(self._path, snapshot)
            except OSError as e:
                if self._events:
                    self._events.emit({
                        "kind": "snapshot_failure", "error": str(e)
                    })
                # Leave dirty set so we retry next round.
                self._dirty.set()


def load_or_init_port_state(snap_path, geometry, emit_event):
    """Build port_state from snapshot (where present) + fresh init (otherwise).

    Returns (port_state_dict, removed_keys_list). emit_event is a callback
    for boot/reload events.
    """
    snapshot = load_snapshot(snap_path)
    port_state = {}
    snapshot_keys = set(snapshot.keys())
    geometry_keys = set()

    for idx, g in enumerate(geometry):
        key = (g["switch"], g["port"])
        geometry_keys.add(key)
        if key in snapshot:
            port_state[key] = snapshot[key]
            # Index might have shifted between snapshots; refresh
            port_state[key]["index"] = idx
            emit_event({
                "kind": "boot", "switch": g["switch"], "port": g["port"],
                "resumed_from_snapshot": True,
            })
        else:
            port_state[key] = new_port_state(
                switch=g["switch"], port=g["port"], ou=g["ou"], index=idx
            )
            emit_event({
                "kind": "boot", "switch": g["switch"], "port": g["port"],
                "resumed_from_snapshot": False,
            })

    removed = sorted(
        f"{sw}:{p}" for (sw, p) in (snapshot_keys - geometry_keys)
    )
    if removed:
        emit_event({"kind": "reload", "reason": "boot_geometry_diff",
                    "added": [], "removed": removed})

    return port_state, removed


# === 4. Switch drivers ======================================================

import base64
import ssl
import urllib.request
import urllib.error


class EosDriver:
    """Arista EOS via eAPI (HTTPS JSON-RPC)."""

    def __init__(self, host, user, password, base_url=None, verify_ssl=False,
                 timeout=EAPI_TIMEOUT_SECS):
        self.host = host
        self.user = user
        self.password = password
        self.base_url = base_url or f"https://{host}/command-api"
        self._timeout = timeout
        if verify_ssl:
            self._ssl_ctx = ssl.create_default_context()
        else:
            self._ssl_ctx = ssl._create_unverified_context()

    def _runcmds(self, cmds):
        body = {
            "jsonrpc": "2.0",
            "method": "runCmds",
            "params": {"version": 1, "cmds": cmds, "format": "json"},
            "id": "switchportrecond",
        }
        auth = base64.b64encode(
            f"{self.user}:{self.password}".encode()
        ).decode()
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {auth}",
            },
        )
        with urllib.request.urlopen(
            req, timeout=self._timeout, context=self._ssl_ctx
        ) as resp:
            return json.loads(resp.read())

    def interfaces_status(self):
        """Returns {port_name: linkStatus} for every port the switch has."""
        out = self._runcmds(["show interfaces status"])
        statuses = out["result"][0]["interfaceStatuses"]
        return {p: meta["linkStatus"] for p, meta in statuses.items()}

    def mac_address_table(self):
        """Returns list of (vlan, mac, port) tuples for dynamic entries."""
        out = self._runcmds(["show mac address-table"])
        entries = out["result"][0]["unicastTable"]["tableEntries"]
        return [
            (e["vlanId"], e["macAddress"].lower(), e["interface"])
            for e in entries
            if e.get("entryType") == "dynamic"
        ]

    def lldp_neighbors(self):
        """Returns {internal_port_name: [{mac, sysname, port_description, mgmt_addrs}]}.

        Skips entries without a MAC-typed chassis ID — those aren't BMCs we
        can correlate against the MAC table. Empty list per port when no
        neighbors are reported; missing-port keys when the switch returns
        no `lldpNeighbors` entry at all (caller treats both as "no LLDP
        info for this port")."""
        out = self._runcmds(["show lldp neighbors detail"])
        neighbors_by_raw_port = out["result"][0].get("lldpNeighbors", {})
        result = {}
        for raw_port, info in neighbors_by_raw_port.items():
            ipn = arista_port_to_internal(raw_port)
            entries = []
            for n in info.get("lldpNeighborInfo", []):
                if n.get("chassisIdType") != "macAddress":
                    continue
                chassis_id = n.get("chassisId", "")
                # Arista renders MACs as dotted-quad ("9803.9ba6.fc24")
                digits = "".join(c for c in chassis_id.lower()
                                 if c in "0123456789abcdef")
                if len(digits) != 12:
                    continue
                mac = ":".join(digits[i:i+2] for i in range(0, 12, 2))
                nbr_iface = n.get("neighborInterfaceInfo") or {}
                mgmt = [
                    a["address"] for a in n.get("managementAddresses", [])
                    if a.get("address")
                ]
                entries.append({
                    "mac": mac,
                    "sysname": n.get("systemName", "") or "",
                    "port_description": nbr_iface.get("interfaceDescription", "") or "",
                    "mgmt_addrs": mgmt,
                })
            result[ipn] = entries
        return result


# Drop-in slots; not implemented in v1.
class CumulusDriver:
    def __init__(self, *a, **kw):
        raise NotImplementedError("CumulusDriver: not implemented in v1")


import re as _re_ios
import subprocess as _sub_ios


class IosDriver:
    """Cisco IOS classic 15.x via SSH+stdin. Read-only — interfaces_status
    and mac_address_table only. The runner callable abstracts ssh for tests.

    Auth: priv-15 user lands directly in privileged exec; no `enable`."""
    def __init__(self, host, user, password, runner=None, timeout=15):
        self.host = host
        self.user = user
        self.password = password
        self._timeout = timeout
        self.runner = runner or self._default_runner

    def _default_runner(self, cmd):
        full = ["sshpass", "-p", self.password, "ssh",
                "-tt",
                "-o", "StrictHostKeyChecking=no",
                "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
                "-o", "KexAlgorithms=+diffie-hellman-group14-sha1",
                "-o", "HostKeyAlgorithms=+ssh-rsa",
                f"{self.user}@{self.host}"]
        return _sub_ios.check_output(full, input=cmd, timeout=self._timeout, text=True)

    def interfaces_status(self):
        """Returns {port_name: linkStatus} matching EosDriver shape.
        IOS column layout: Port Name Status Vlan Duplex Speed Type — port is
        leftmost token, Status is column 3 in the 'connected/notconnect/...'
        space."""
        out = self.runner("show interfaces status")
        statuses = {}
        for line in out.splitlines():
            if not line or line.startswith("Port") or line.startswith("---"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            port = parts[0]
            if not _re_ios.match(r"(Gi|Te|Fa|Tw|Hu)\d", port):
                continue
            # Name column is variable-width and can be multi-word ("Rabbit-1
            # (Wedge100", "Guest computer mgm"), so split() drops Status into
            # an indeterminate position. Scan all tokens after the port for
            # the first known status keyword.
            known = {"connected", "notconnect", "disabled", "err-disabled",
                     "monitoring", "suspended"}
            for tok in parts[1:]:
                if tok in known:
                    statuses[port] = tok
                    break
        return statuses

    def mac_address_table(self):
        """Returns list of (vlan_int, mac_lower_colon, port) tuples for
        DYNAMIC entries only (skips STATIC/CPU)."""
        out = self.runner("show mac address-table")
        entries = []
        for line in out.splitlines():
            parts = line.split()
            # Need: vlan dotted-mac type port — at least 4 tokens.
            if len(parts) < 4:
                continue
            if "." not in parts[1]:
                continue
            if parts[2].upper() != "DYNAMIC":
                continue
            try:
                vlan = int(parts[0])
            except ValueError:
                continue
            mac_dotted = parts[1].lower()
            mac_colon = ":".join(
                mac_dotted.replace(".", "")[i:i+2] for i in range(0, 12, 2))
            entries.append((vlan, mac_colon, parts[3]))
        return entries


DRIVERS = {"eos": EosDriver, "cumulus": CumulusDriver, "ios": IosDriver}


def make_driver(switch, credentials):
    """Construct a driver from a switches.json entry + credentials.json dict."""
    drv_cls = DRIVERS[switch["driver"]]
    if switch["driver"] == "eos":
        return drv_cls(
            host=switch["host"],
            user=credentials["eosuser"],
            password=credentials["eospass"],
        )
    if switch["driver"] == "ios":
        return drv_cls(
            host=switch["host"],
            user=credentials["cisco_user"],
            password=credentials["cisco_pass"],
        )
    raise NotImplementedError(f"driver {switch['driver']!r}")

# === 5. Switch fetcher ======================================================


def arista_port_to_internal(name):
    """Ethernet6/1 → et6b1; Ethernet1 → ethernet1 (non-breakout passthrough)."""
    m = re.match(r"^Ethernet(\d+)/(\d+)$", name)
    if m:
        return f"et{m.group(1)}b{m.group(2)}"
    return name.lower().replace(" ", "")


def switch_fetch_once(driver):
    """One cycle of switch polling.

    Returns (facts_dict, error_str_or_None) where facts_dict is keyed
    by internal port name (et6b1 etc.) with values
    {linkstate, macs[]}. Partial failures populate what they can and
    return an error string; a fully-failed call returns ({}, error).
    """
    facts = {}
    error_parts = []

    try:
        statuses = driver.interfaces_status()
    except Exception as e:
        statuses = {}
        error_parts.append(f"interfaces_status: {e!r}")

    for raw_port, link in statuses.items():
        ipn = arista_port_to_internal(raw_port)
        facts[ipn] = {"linkstate": link, "macs": []}

    try:
        table = driver.mac_address_table()
        for vlan, mac, raw_port in table:
            ipn = arista_port_to_internal(raw_port)
            if ipn not in facts:
                facts[ipn] = {"linkstate": "unknown", "macs": []}
            facts[ipn]["macs"].append(mac.lower())
    except Exception as e:
        error_parts.append(f"mac_address_table: {e!r}")

    # LLDP neighbors — corroborates which MAC on a port is the BMC.
    # Independent try/except so a partial LLDP failure doesn't void
    # the MAC table data we already collected.
    try:
        lldp_by_port = (driver.lldp_neighbors()
                        if hasattr(driver, "lldp_neighbors") else {})
    except Exception as e:
        lldp_by_port = {}
        error_parts.append(f"lldp_neighbors: {e!r}")

    for ipn, fact in facts.items():
        fact["lldp_neighbors"] = lldp_by_port.get(ipn, [])
    for ipn, neighbors in lldp_by_port.items():
        if ipn not in facts:
            facts[ipn] = {"linkstate": "unknown", "macs": [],
                          "lldp_neighbors": neighbors}

    error = "; ".join(error_parts) if error_parts else None
    return facts, error


def _update_lldp_snapshot(switch_name, facts, store):
    """Update `store[switch_name]` with the LLDP-by-port map derived from
    `facts`, but ONLY when the new map is non-empty.

    On a failed/empty fetch cycle we preserve the previous snapshot so
    downstream readers see staleness (via LLDP_CACHE_MAX_AGE_SECS) rather
    than a misleading "fresh empty" that masquerades as authoritative
    absence of LLDP data. Caller is responsible for holding the lock that
    guards `store`.
    """
    new_lldp = {
        port: info.get("lldp_neighbors", [])
        for port, info in facts.items()
    }
    if new_lldp:
        store[switch_name] = new_lldp
    # Else: preserve previous snapshot — readers fall back to last-known
    # good until LLDP_CACHE_MAX_AGE_SECS expires.


class SwitchFetcher:
    """Background thread that polls one switch every cycle_secs."""

    def __init__(self, switch_name, driver, switch_facts, switch_facts_lock,
                 emit_event, cycle_secs=CYCLE_SECS):
        self._name = switch_name
        self._driver = driver
        self._facts = switch_facts
        self._facts_lock = switch_facts_lock
        self._emit = emit_event
        self._cycle = cycle_secs
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"switch-fetcher-{switch_name}", daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self):
        consecutive_failures = 0
        while not self._stop.is_set():
            facts, error = switch_fetch_once(self._driver)
            if error:
                consecutive_failures += 1
                self._emit({"kind": "fetcher_failure", "switch": self._name,
                            "error": error})
            else:
                consecutive_failures = 0
            with self._facts_lock:
                for port, info in facts.items():
                    self._facts[(self._name, port)] = info
                _update_lldp_snapshot(self._name, facts, _LLDP_BY_SWITCH)
            # Backoff on consecutive failures (jitter): 0.5..1.5x cycle
            import random
            jitter = 1.0 if consecutive_failures == 0 else random.uniform(0.5, 1.5)
            self._stop.wait(self._cycle * jitter)


# === 6. Port worker (12-var state machine) ==================================

import socket
import subprocess
import os as _os_mod


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


# --- BMC probe primitives -----------------------------------------------------


def _tcp_port_open(host, port, timeout=1.0):
    """True iff a TCP connect to host:port completes within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _ipmi_responsive(host, timeout=2.0):
    """True iff an IPMI handshake to host:623 elicits any response.

    Sends `channel info` with throwaway creds — any non-timeout return
    means a listener is there. Replaces nmap_ipmi (Task 16) with a probe
    that doesn't depend on `nmap` being installed.
    """
    try:
        subprocess.run(
            ["ipmitool", "-I", "lanplus", "-N", "1", "-R", "0",
             "-U", "x", "-P", "x", "-H", host, "channel", "info"],
            timeout=timeout, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _default_ssh_runner(host, user, password, cmd, timeout=SSH_TIMEOUT_SECS):
    """sshpass + ssh, returns stdout text. Caller catches exceptions."""
    full = ["sshpass", "-p", password, "ssh",
            "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
            "-o", "ConnectTimeout=3",
            f"{user}@{host}", cmd]
    return subprocess.check_output(full, timeout=timeout, text=True,
                                   stderr=subprocess.DEVNULL)


import time as _time_mod
import tempfile

_REDFISH_RESET_LOCK = threading.Lock()
_LAST_REDFISH_RESET = {}  # bmc_ip → time.time() of last fire


def _parse_sentinel_kv(path):
    """Parse a key=value sentinel file. Returns {} on any read/parse
    failure (caller treats as inactive)."""
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
        _os_mod.unlink(path)
    except FileNotFoundError:
        pass


def intentional_flap_active(switch, port, intentional_flap_dir=None,
                            now_fn=None):
    """True iff <dir>/<switch>/<port> holds a fresh flap sentinel.

    The file is a small key=value record written by an actor (today:
    reaper-leased's kick_via_switch_flap) BEFORE it intentionally
    bounces the port. While the sentinel is fresh, the consumer must
    freeze per-port state — no linkstate transition recorded, no var
    reset, no IPMI/ssh probe — so that our own deliberate flap doesn't
    invalidate hard-won observability state (chassis_sn latch,
    bmc_kind_cached, inventory).

    Freshness rule: `until` (unix-ts) must satisfy
        now ≤ until ≤ now + MAX_FLAP_HOLD_SECS
    Out-of-range values are stale (past) or producer-bug (too far
    future); both cases unlink the file so the directory doesn't
    accumulate dead state. Missing `until`, malformed lines, parse
    errors, or any unlink-able-but-unparsable file ⇒ inactive.

    Producer and consumer agree on no other key; future fields
    (`reason`, `mac`, `pid`) are advisory and ignored here.
    """
    if intentional_flap_dir is None:
        intentional_flap_dir = INTENTIONAL_FLAP_DIR
    if now_fn is None:
        now_fn = _time_mod.time
    path = _os_mod.path.join(intentional_flap_dir, switch, port)
    if not _os_mod.path.exists(path):
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


def sweep_stale_flap_sentinels(intentional_flap_dir=None, now_fn=None):
    """One-shot janitor for the intentional-flap sentinel directory.

    Called at switchportrecond startup: walks <dir>/<switch>/<port>
    and unlinks every file whose `until` is in the past OR whose
    contents are unparseable (missing/garbage). Fresh sentinels (a
    flap was in progress when the daemon restarted) are preserved.

    Returns the count of files unlinked. Missing directory is a no-op
    (returns 0) — on a freshly-booted bang the tmpfs subdir may not
    exist until the first producer write.
    """
    if intentional_flap_dir is None:
        intentional_flap_dir = INTENTIONAL_FLAP_DIR
    if now_fn is None:
        now_fn = _time_mod.time
    if not _os_mod.path.isdir(intentional_flap_dir):
        return 0
    now = int(now_fn())
    swept = 0
    for sw_name in _os_mod.listdir(intentional_flap_dir):
        sw_dir = _os_mod.path.join(intentional_flap_dir, sw_name)
        if not _os_mod.path.isdir(sw_dir):
            continue
        for port_name in _os_mod.listdir(sw_dir):
            path = _os_mod.path.join(sw_dir, port_name)
            if not _os_mod.path.isfile(path):
                continue
            kv = _parse_sentinel_kv(path)
            try:
                until = int(kv["until"])
            except (KeyError, ValueError):
                _unlink_quiet(path)
                swept += 1
                continue
            if until < now:
                _unlink_quiet(path)
                swept += 1
    return swept


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
    path = _os_mod.path.join(sol_active_dir, bmc_ip)
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        _os_mod.kill(pid, 0)
        return True
    except FileNotFoundError:
        return False
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        try:
            _os_mod.unlink(path)
        except FileNotFoundError:
            pass
        return False


def write_lldp_cache(facts_per_switch, path=None):
    """Atomically replace LLDP_CACHE_PATH with the latest per-switch view.

    facts_per_switch: {switch_name: {port: [neighbor_dict]}}.
    Writes via tmp+rename so a partial write never leaves readers with
    a corrupt file. Creates the parent directory if needed (first-boot
    on a fresh bang).
    """
    if path is None:
        path = LLDP_CACHE_PATH
    payload = {
        "_updated_at": _ts_now(),
        "switches": facts_per_switch,
    }
    parent = _os_mod.path.dirname(path) or "."
    _os_mod.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    _os_mod.replace(tmp, path)


def _load_redfish_credentials(path=None):
    """Load /etc/flax/credentials-redfish.json — list of {bmcuser, bmcpass}.
    Returns [] on any error; the auto-fire path is best-effort and a
    missing/malformed file is not a polling-cycle failure."""
    if path is None:
        path = REDFISH_CREDENTIALS_PATH
    try:
        with open(path) as f:
            first = f.readline()
            if first.startswith("$ANSIBLE_VAULT"):
                return []
            data = json.loads(first + f.read())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [c for c in data if "bmcuser" in c and "bmcpass" in c]


def _maybe_fire_redfish_reset(bmc_ip):
    """Background-spawn `bmc-reset-via-redfish` for bmc_ip, rate-limited.

    Triggered from `_default_ipmi_runner` when the BMC returns
    'insufficient resources for session' — the AMI MegaRAC session-
    table-exhaustion symptom. Manager.Reset (ForceRestart) clears the
    table; ~3 min BMC outage with the host CPU left running.

    Rate-limited per-BMC to one fire per REDFISH_RESET_RATE_LIMIT_SECS
    so a misbehaving BMC that re-fills the session table immediately
    can't be reset-thrashed. Fire-and-forget via Popen — the recovery
    script blocks 3–5 min waiting for the BMC to come back; the poll
    cycle must not.
    """
    if not bmc_ip:
        return False
    now = _time_mod.time()
    with _REDFISH_RESET_LOCK:
        last = _LAST_REDFISH_RESET.get(bmc_ip, 0)
        if now - last < REDFISH_RESET_RATE_LIMIT_SECS:
            return False
        _LAST_REDFISH_RESET[bmc_ip] = now
    if not _os_mod.path.exists(REDFISH_RESET_BIN):
        return False
    creds = _load_redfish_credentials()
    cmd = [REDFISH_RESET_BIN, bmc_ip]
    if creds:
        cmd += [creds[0]["bmcuser"], creds[0]["bmcpass"]]
    try:
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except Exception:
        return False


_IPMI_SESSION_EXHAUSTION_PATTERN = b"insufficient resources for session"


def _default_ipmi_runner(host, user, password, args,
                         timeout=IPMITOOL_TIMEOUT_SECS):
    """Single ipmitool invocation, returns stdout text. Caller catches.

    Tries cipher suite 3 first (the historical default that works for
    Wedge OpenBMC and most traditional IPMI BMCs). If the BMC rejects
    it with 'invalid authentication algorithm' (common on newer
    Tioga Pass openBMC), falls back to no explicit -C flag — that
    lets ipmitool auto-negotiate the cipher.

    When both attempts fail and the combined stderr contains
    'insufficient resources for session' (AMI MegaRAC RMCP+ session
    table full), fires `bmc-reset-via-redfish` in the background to
    recover the BMC. Rate-limited; see `_maybe_fire_redfish_reset`.
    """
    common = ["-I", "lanplus", "-N", "2", "-R", "3",
              "-U", user, "-P", password, "-H", host]
    last_err = b""
    try:
        result = subprocess.run(
            ["ipmitool", "-C", "3"] + common + args,
            timeout=timeout, capture_output=True, check=True,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        last_err = e.stderr or b""
    try:
        result = subprocess.run(
            ["ipmitool"] + common + args,
            timeout=timeout, capture_output=True, check=True,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        last_err = last_err + b"\n" + (e.stderr or b"")
        if _IPMI_SESSION_EXHAUSTION_PATTERN in last_err.lower():
            _maybe_fire_redfish_reset(host)
        raise


def _parse_fru_product_name(fru_text):
    """Pull 'Product Name : <value>' from ipmitool fru output."""
    for line in fru_text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        if k.strip() == "Product Name":
            return v.strip()
    return None


def _serial_from_fru(fru_text):
    """Pull 'Product Serial' (preferred) or 'Chassis Serial' (fallback)
    from `ipmitool fru` output.

    Skips empty values: ipmitool fru on a chassis with multiple FRU
    devices (e.g. Tioga Pass + Ava-M.2-SSD-Adap carrier) emits one
    'Product Serial' line per FRU. The carrier card's value is empty
    on most boards, and depending on enumeration order it can shadow
    the real builtin-FRU value. Walking past empty matches gets us to
    the first FRU that actually has a serial set."""
    for needle in ("Product Serial", "Chassis Serial"):
        for line in fru_text.splitlines():
            if needle in line:
                v = line.split(":", 1)[1].strip()
                if v:
                    return v
    return None


def probe_bmc_kind(ip, credentials, bmc_creds,
                   ssh_runner=None, ipmi_runner=None, port_probe=None):
    """Identify what kind of BMC sits at `ip`.

    Returns {"kind": "openbmc"|"traditional"|"unknown",
             "product_name": str|None, "creds_used": (user, pass)|None}.

    Strategy:
      1. Probe TCP:22 + UDP:623 to fast-fail non-BMC IPs (~3s budget).
      2. If ssh:22 open AND `cat /etc/os-release` contains 'openbmc':
         openbmc path with credentials['obmcuser'/'obmcpass'].
      3. Elif udp:623 responsive: walk bmc_creds via ipmitool fru.
      4. Else: 'unknown' (closed and unknown are binned together).
    """
    if ssh_runner is None:
        ssh_runner = _default_ssh_runner
    if ipmi_runner is None:
        ipmi_runner = _default_ipmi_runner
    if port_probe is None:
        port_probe = lambda h: {
            "ssh":  _tcp_port_open(h, 22, timeout=1.0),
            "ipmi": _ipmi_responsive(h, timeout=2.0),
        }

    ports = port_probe(ip)

    if ports.get("ssh"):
        try:
            rel = ssh_runner(ip, credentials["obmcuser"],
                             credentials["obmcpass"], "cat /etc/os-release")
            if "openbmc" in rel.lower():
                pn = None
                try:
                    fru = ssh_runner(
                        ip, credentials["obmcuser"], credentials["obmcpass"],
                        "ipmitool fru 2>/dev/null || cat /run/fru")
                    pn = _parse_fru_product_name(fru)
                except Exception:
                    pass
                return {"kind": "openbmc", "product_name": pn,
                        "creds_used": (credentials["obmcuser"],
                                       credentials["obmcpass"])}
        except Exception:
            pass

    if ports.get("ipmi"):
        for c in bmc_creds:
            try:
                fru = ipmi_runner(ip, c["bmcuser"], c["bmcpass"], ["fru"])
                pn = _parse_fru_product_name(fru)
                return {"kind": "traditional", "product_name": pn,
                        "creds_used": (c["bmcuser"], c["bmcpass"])}
            except Exception:
                continue

    return {"kind": "unknown", "product_name": None, "creds_used": None}


def bmc_power_status_traditional(ip, creds_pair):
    """ipmitool -U user -P pw -H ip power status → 'on'/'off'/'unknown'."""
    try:
        out = _default_ipmi_runner(ip, creds_pair[0], creds_pair[1],
                                   ["power", "status"])
    except Exception:
        return "unknown"
    o = out.lower()
    if "is on" in o:
        return "on"
    if "is off" in o:
        return "off"
    return "unknown"


def bmc_power_status_openbmc(ip, creds_pair):
    """ssh root@bmc 'ipmitool power status'. Same parse as traditional."""
    try:
        out = _default_ssh_runner(ip, creds_pair[0], creds_pair[1],
                                  "ipmitool power status")
    except Exception:
        return "unknown"
    o = out.lower()
    if "is on" in o:
        return "on"
    if "is off" in o:
        return "off"
    return "unknown"


def chassis_serial_traditional(ip, creds_pair):
    """ipmitool fru → Product Serial (or Chassis Serial fallback)."""
    try:
        out = _default_ipmi_runner(ip, creds_pair[0], creds_pair[1], ["fru"])
    except Exception:
        return None
    return _serial_from_fru(out)


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
      - a BMC kind flip (e.g. firmware update changing openbmc→traditional)
        re-fires too,
      - daemon restart re-fires once and then leaves alone.

    Failures emit an `inband_admin_setup_failed` event with the
    repr'd error; the sentinel is NOT set, so a later transition
    retries. Polling cycle never fails on this — the booted-host
    power-off path it unblocks is a downstream concern.

    `runner` is injectable for tests; production passes None and the
    helper shells out to BMC_ENABLE_INBAND_ADMIN_BIN."""
    if kind not in ("openbmc", "traditional"):
        return
    if not bmc_ip or not creds_used:
        return
    sentinel_key = (bmc_mac, kind)
    if port_state.get("inband_admin_configured_for") == sentinel_key:
        return
    user, password = creds_used
    if runner is None:
        if not _os_mod.path.exists(BMC_ENABLE_INBAND_ADMIN_BIN):
            return  # tool not deployed on this host yet — apply role pending
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


def chassis_serial_openbmc(ip, creds_pair):
    """ssh + (ipmitool fru || cat /run/fru) → Product/Chassis Serial."""
    try:
        out = _default_ssh_runner(ip, creds_pair[0], creds_pair[1],
                                  "ipmitool fru 2>/dev/null || cat /run/fru")
    except Exception:
        return None
    return _serial_from_fru(out)


def _parse_power_from_ipmi_output(text):
    """'on'/'off'/'unknown' from any line containing 'is on'/'is off'."""
    o = text.lower()
    if "is on" in o:
        return "on"
    if "is off" in o:
        return "off"
    return "unknown"


def _parse_watts_from_ipmi_output(text):
    """'NNN W' from the HSC Input Power line of an `ipmitool sdr` dump."""
    for line in text.splitlines():
        if "hsc input power" in line.lower():
            parts = line.split("|")
            if len(parts) >= 2:
                return parts[1].strip().replace(" Watts", " W")
    return None


def bmc_power_and_sdr_traditional(ip, creds_pair):
    """One-shot replacement for `bmc_power_status_traditional` +
    `bmc_input_power_traditional`. Both commands run in a SINGLE RMCP+
    session via `ipmitool ... exec FILE` — ipmitool keeps the same
    interface handle open across every line of the exec script.

    Cuts the per-poll BMC RMCP+ session count from 3 to 2 on AMI
    BMCs, which leaves more headroom in the BMC's session table for
    a long-running soltriage SOL session (the original motivation —
    eindhoven 2026-05-20).

    Returns (power, watts) where power ∈ {'on','off','unknown'} and
    watts is 'NNN W' or None.
    """
    pwr = "unknown"
    watts = None
    tmp = tempfile.NamedTemporaryFile(
        mode="w", prefix="ipmi-cmds-", suffix=".txt", delete=False)
    try:
        tmp.write("power status\nsdr\n")
        tmp.close()
        try:
            out = _default_ipmi_runner(ip, creds_pair[0], creds_pair[1],
                                       ["exec", tmp.name])
        except Exception:
            return ("unknown", None)
        pwr = _parse_power_from_ipmi_output(out)
        if pwr == "on":
            watts = _parse_watts_from_ipmi_output(out)
    finally:
        try:
            _os_mod.unlink(tmp.name)
        except FileNotFoundError:
            pass
    return (pwr, watts)


def bmc_input_power_traditional(ip, creds_pair):
    """ipmitool sdr → 'NNN W' (HSC Input Power line) or None."""
    try:
        out = _default_ipmi_runner(ip, creds_pair[0], creds_pair[1], ["sdr"])
    except Exception:
        return None
    for line in out.splitlines():
        if "hsc input power" in line.lower():
            parts = line.split("|")
            if len(parts) >= 2:
                return parts[1].strip().replace(" Watts", " W")
    return None


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


def _link_value_from_eapi(linkstate):
    """eAPI 'connected'/'notconnect'/'disabled' → 'link'/'nolink'/'unknown'."""
    if linkstate == "connected":
        return "link"
    if linkstate in ("notconnect", "disabled"):
        return "nolink"
    return "unknown"


def lookup_lease_ip(leases_path, mac, dhcp_hosts_dir=None):
    """Get the IP for a MAC from dnsmasq's view.

    Scans dnsmasq's active leases file first (live, dynamically-leased
    state). If the MAC is not there and dhcp_hosts_dir is provided, scans
    every regular file in that directory for static reservations of form
    'mac,ip,name' per line.

    Statically-configured BMCs (locally pinned to their reservation IP
    via the BMC web UI rather than DHCP) never appear in leases but do
    appear in dhcp-hosts, so checking both is necessary to resolve them.

    leases file format: '<expiry> <mac> <ip> <hostname> <client_id>'.
    dhcp-hosts file format: 'mac,ip[,name[,...]]' per line; '#' comments.

    Returns the IP string, or None if the MAC is in neither place.
    """
    if not mac:
        return None
    target = mac.lower()
    try:
        with open(leases_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1].lower() == target:
                    return parts[2]
    except FileNotFoundError:
        pass
    if dhcp_hosts_dir:
        try:
            entries = sorted(_os_mod.listdir(dhcp_hosts_dir))
        except (FileNotFoundError, NotADirectoryError):
            return None
        for entry in entries:
            path = _os_mod.path.join(dhcp_hosts_dir, entry)
            if not _os_mod.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        fields = line.split(",")
                        if len(fields) >= 2 and fields[0].lower() == target:
                            # Pick the first dotted-decimal v4 from the
                            # remaining fields (handles MAC,IP,NAME and the
                            # MAC,set:tag,IP,NAME variant).
                            for fld in fields[1:]:
                                if "." in fld and fld.replace(".", "").isdigit():
                                    return fld
                            return fields[1]
            except OSError:
                continue
    return None


def nginx_pxe_seen(access_log_path, node_ip, tail_lines=100):
    """Did node_ip recently fetch a liveiso payload?

    Mirrors the bash predecessor:
        grep '/suse/live/test/LiveLeap' <access_log> | tail -n 100 | grep <ip>
    Filter-by-needle FIRST (so older hits aren't missed when the log is
    busy), tail the 100 most recent needle matches, then check the IP as
    a substring (not just at line-start) — that's what `grep $ip` does.
    """
    if not node_ip:
        return "unknown"
    needle = "/suse/live/test/LiveLeap"
    try:
        with open(access_log_path) as f:
            matches = [ln for ln in f if needle in ln]
    except FileNotFoundError:
        return "unknown"
    for line in matches[-tail_lines:]:
        if node_ip in line:
            return "found"
    return "notfound"


def inventory_status(nodes_root, nic_mac, link_changed_ts):
    """Has /export/nodes/post-<mac>/latest been collected in the CURRENT
    link session?

    Returns 'found' iff the file exists AND its mtime is newer than
    `link_changed_ts` (the linkstate var's `since` timestamp, which only
    advances on link-state transitions — not on every poll). 'notfound'
    if the file is missing or predates the current link session;
    'unknown' if there's no NIC MAC yet.

    The mtime-vs-link-ts comparison is load-bearing, NOT a freshness
    heuristic: linkstate.since marks when the current 'link' value was
    established (i.e. when the port came up). If the inventory file
    predates that, the file was written during a PRIOR link session and
    there's been at least one link-down→link-up transition since. A
    reslot (or any physical re-insertion that preserves the NIC MAC but
    changes the underlying chassis — same MAC swapped into a different
    machine, NIC card moved, etc.) is invisible to the bmc_mac /
    chassis_sn signals when only the NIC end is replaced, so this
    timestamp check is the only thing that forces re-inventory after a
    link-session boundary. Don't soften it.
    """
    if not nic_mac:
        return "unknown"
    fn = "post-" + nic_mac.replace(":", "")
    latest = _os_mod.path.join(nodes_root, fn, "latest")
    try:
        mtime = _os_mod.path.getmtime(latest)
    except OSError:
        return "notfound"
    if not link_changed_ts:
        return "found"
    link_ts = datetime.datetime.strptime(
        link_changed_ts, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=datetime.timezone.utc).timestamp()
    return "found" if mtime > link_ts else "notfound"


def ssh_uptime(ip, host_creds, timeout=SSH_TIMEOUT_SECS):
    """ssh-runs `uptime` walking host_creds; returns 'ok' / 'fail' / 'unknown'.

    Walks a list of {user, pass} dicts (loaded from
    /etc/flax/credentials-host.json — same shape reaper-leased uses).
    First credential whose ssh returns rc=0 → "ok". All tried + none
    succeeded → "fail". No ip or no creds → "unknown".

    Mirrors reaper-leased's host-cred walk pattern. The bash predecessor
    used a single sshuser/sshpass tuple from credentials.json; modern
    deployments split that into a walkable list under credentials-host.json
    so a node provisioned with a non-default OS (different default user)
    can still be probed without per-site config drift.
    """
    if not ip:
        return "unknown"
    if not host_creds:
        return "unknown"
    tried_any = False
    for c in host_creds:
        try:
            cp = subprocess.run(
                ["sshpass", "-p", c["pass"], "ssh",
                 "-tt",
                 "-o", "StrictHostKeyChecking=no",
                 "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
                 "-o", f"ConnectTimeout={timeout}",
                 "-l", c["user"], ip, "uptime"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout + 2,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            tried_any = True
            continue
        tried_any = True
        if cp.returncode == 0:
            return "ok"
    return "fail" if tried_any else "unknown"


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


def port_worker_one_iter(port_state, switch_facts, emit_event, env):
    """Run one polling iteration of the 12-var state machine for one port.

    `switch_facts` is the latest snapshot from the switch fetcher (per-port
    dict of {linkstate, macs}). `env` is a dict of host-side helpers that
    later tasks plug in (lease lookup, ipmitool runner, etc.).
    """
    port = port_state["port"]
    fact = switch_facts.get(port)
    if fact is None:
        _set_var(port_state, "linkstate", "unknown", emit_event)
        port_state["last_polled"] = _ts_now()
        return
    new_link = _link_value_from_eapi(fact["linkstate"])

    # Freeze per-port state when another actor (today: reaper-leased's
    # kick_via_switch_flap) has signalled an intentional flap on this
    # port. We're the cause; the chassis is still there; don't bump
    # linkstate.since (which would invalidate the inventory mtime check)
    # and don't run probes that'll just time out while the port bounces.
    # Emit a forensic event only if linkstate WOULD have transitioned —
    # quiet during cycles that happen to catch the port still up.
    if intentional_flap_active(port_state["switch"], port_state["port"]):
        if port_state["vars"]["linkstate"]["value"] != new_link:
            emit_event({
                "kind": "intentional_flap_observed",
                "switch": port_state["switch"],
                "port": port_state["port"],
                "raw_linkstate": fact["linkstate"],
                "would_have_been": new_link,
            })
        port_state["last_polled"] = _ts_now()
        return

    _set_var(port_state, "linkstate", new_link, emit_event)
    port_state["last_polled"] = _ts_now()

    if port_state["vars"]["linkstate"]["value"] != "link":
        # Without link, the BMC discovery path is short-circuited.
        for v in ("bmcmac", "bmcip", "bmcping", "bmcipmi", "bmcpower",
                  "chassissn", "nodeip", "nodeping", "nodepxe",
                  "nodessh", "inventory"):
            _set_var(port_state, v, "unknown", emit_event)
        # Cache invalidation: link drop is the signal that the chassis
        # may have been physically swapped, so forget the BMC kind cache
        # and the latched chassis serial. They re-acquire on link-up.
        port_state.pop("bmc_kind_cached", None)
        port_state["chassis_sn"] = None
        # Display "?" — the previous watts are no longer trustworthy and
        # "0 W" would mislead operators into thinking the chassis is off
        # rather than gone.
        port_state["bmc_power"] = "?"
        port_state["bmcpower_unknown_streak"] = 0
        port_state["bmcpower_stale_since"] = None
        return

    # bmcmac: classify the per-port macs into BMC + NIC(s) + junk
    macs = fact["macs"]
    classified = classify_macs(macs, lldp_neighbors=fact.get("lldp_neighbors", []))
    port_state["nic_macs"] = list(classified.nics)
    port_state["classification_source"] = classified.classification_source
    port_state["lldp_disagreement"] = classified.lldp_disagreement
    if classified.bmc:
        port_state["bmc_mac"] = classified.bmc
        port_state["nic_mac"] = classified.nics[0] if classified.nics else None
        _set_var(port_state, "bmcmac", "found", emit_event)
    else:
        port_state["bmc_mac"] = None
        port_state["nic_mac"] = None
        _set_var(port_state, "bmcmac", "notfound", emit_event)
    _persist_junk_macs(port_state, classified.junk, emit_event)

    leases_path = env.get("leases_path", "/var/lib/misc/dnsmasq.leases")
    dhcp_hosts_dir = env.get("dhcp_hosts_dir", "/etc/dnsmasq.dhcp-hosts")

    if port_state["bmc_mac"]:
        bmcip = lookup_lease_ip(leases_path, port_state["bmc_mac"], dhcp_hosts_dir)
        if bmcip:
            port_state["bmc_ip"] = bmcip
            _set_var(port_state, "bmcip", "found", emit_event)
        else:
            port_state["bmc_ip"] = None
            _set_var(port_state, "bmcip", "notfound", emit_event)
    else:
        _set_var(port_state, "bmcip", "unknown", emit_event)

    if port_state["nic_mac"]:
        nicip = lookup_lease_ip(leases_path, port_state["nic_mac"], dhcp_hosts_dir)
        if nicip:
            port_state["nic_ip"] = nicip
            _set_var(port_state, "nodeip", "found", emit_event)
        else:
            port_state["nic_ip"] = None
            _set_var(port_state, "nodeip", "notfound", emit_event)
    else:
        _set_var(port_state, "nodeip", "unknown", emit_event)

    _set_var(port_state, "bmcping", ping_host(port_state["bmc_ip"]), emit_event)
    _set_var(port_state, "nodeping", ping_host(port_state["nic_ip"]), emit_event)

    # bmcipmi (kind) + bmcpower + chassissn
    bmc_ip      = port_state["bmc_ip"]
    bmc_mac     = port_state.get("bmc_mac")
    credentials = env.get("credentials", {})
    bmc_creds   = env.get("bmc_credentials", [])

    if not bmc_ip:
        port_state.pop("bmc_kind_cached", None)
        # Display "?" — without a BMC IP the previous watts are not a
        # trustworthy read of current chassis state.
        port_state["bmc_power"] = "?"
        port_state["bmcpower_stale_since"] = None
        _set_var(port_state, "bmcipmi",   "unknown", emit_event)
        _set_var(port_state, "bmcpower",  "unknown", emit_event)
        _set_var(port_state, "chassissn", "unknown", emit_event)
    else:
        cache = port_state.get("bmc_kind_cached")
        mac_changed = bool(cache) and cache.get("for_mac") != bmc_mac
        # Re-probe when:
        #   - never probed (cache missing)
        #   - bmc_mac changed (different chassis on the same port)
        #   - last probe returned "unknown" (transient BMC unresponsiveness:
        #     don't latch a negative result forever — that locks the port
        #     into perpetual notfound even after the BMC recovers).
        needs_reprobe = (
            cache is None
            or mac_changed
            or cache.get("kind") == "unknown"
        )
        if needs_reprobe:
            probe = probe_bmc_kind(bmc_ip, credentials, bmc_creds)
            cache = {"kind": probe["kind"],
                     "creds_used": probe["creds_used"],
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
        _set_var(port_state, "bmcipmi", kind, emit_event)
        # New BMC classification → grant in-band admin priv (KCS) so the
        # booted host's `ipmitool chassis power off` works. Idempotent;
        # sentinel skips re-runs once configured.
        _ensure_inband_admin_configured(
            port_state, bmc_ip, bmc_mac, kind, creds_used, emit_event)

        prev_streak = port_state.get("bmcpower_unknown_streak", 0)
        sn = None
        pwr = "unknown"
        watts = None
        if kind == "openbmc" and creds_used:
            pwr = bmc_power_status_openbmc(bmc_ip, creds_used)
            sn = chassis_serial_openbmc(bmc_ip, creds_used)
            # Some openbmc BMCs (e.g. Tioga Pass) don't expose chassis
            # or product serial via the SSH FRU path that
            # chassis_serial_openbmc uses, but DO expose them via
            # traditional IPMI on UDP:623. Walk bmc_creds for a working
            # pair before giving up.
            if sn is None:
                for c in bmc_creds:
                    sn = chassis_serial_traditional(
                        bmc_ip, (c["bmcuser"], c["bmcpass"]))
                    if sn is not None:
                        break
        elif kind == "traditional" and creds_used:
            # Mitigation 1: skip if soltriage holds an SOL session on this
            # BMC — competing RMCP+ sessions evict the SOL slot on AMI
            # MegaRAC's small (4–8 slot) session table.
            if _sol_session_active(bmc_ip):
                emit_event({
                    "kind": "sol_active_skip",
                    "switch": port_state["switch"],
                    "port": port_state["port"],
                    "bmc_ip": bmc_ip,
                })
                # leave bmcpower + chassissn latched at last value
                pwr = port_state["vars"]["bmcpower"]["value"]
                sn = None
            else:
                # Mitigation 3: one RMCP+ session for both `power status`
                # and `sdr` via ipmitool's `exec` script form.
                pwr, watts = bmc_power_and_sdr_traditional(bmc_ip, creds_used)
                # Mitigation 2: Product Serial is invariant for a given
                # chassis. Once latched, skip the refetch every cycle —
                # saves one RMCP+ session per poll. Hardware swap clears
                # chassis_sn above (mac_changed branch), forcing refetch.
                if port_state.get("chassis_sn") and not mac_changed:
                    sn = None  # latch keeps the existing value below
                else:
                    sn = chassis_serial_traditional(bmc_ip, creds_used)

        # Latch decision: while the BMC is identified (chassis_sn latched)
        # and still pingable (bmcping=ok), a single failed IPMI power poll
        # shouldn't flip bmcpower to unknown — it's almost always a
        # transient session-table or RMCP+ glitch on the BMC. Hold the
        # previous on/off value until the wider context (bmcping, mac,
        # chassis_sn, link) actually breaks.
        bmcping_value = port_state["vars"]["bmcping"]["value"]
        prev_pwr = port_state["vars"]["bmcpower"]["value"]
        chassis_sn_latched = bool(port_state.get("chassis_sn"))
        latch_eligible = (
            pwr == "unknown"
            and bmcping_value == "ok"
            and chassis_sn_latched
            and prev_pwr in ("on", "off")
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
                    "switch": port_state["switch"],
                    "port": port_state["port"],
                    "bmc_ip": bmc_ip,
                    "bmc_mac": bmc_mac,
                    "consecutive_unknowns": 3,
                    "latched": True,
                })
        else:
            _set_var(port_state, "bmcpower", pwr, emit_event)
            if pwr == "on" and watts:
                port_state["bmc_power"] = watts
                port_state["bmcpower_stale_since"] = None
            elif pwr == "off":
                port_state["bmc_power"] = "0 W"
                port_state["bmcpower_stale_since"] = None
            elif pwr == "unknown":
                # Latch broken (bmcping not ok, or chassis_sn cleared).
                port_state["bmc_power"] = "?"
                port_state["bmcpower_stale_since"] = None

            if pwr == "unknown":
                port_state["bmcpower_unknown_streak"] = prev_streak + 1
                if port_state["bmcpower_unknown_streak"] == 3:
                    emit_event({
                        "kind": "bmc_poll_failed",
                        "switch": port_state["switch"],
                        "port": port_state["port"],
                        "bmc_ip": bmc_ip,
                        "bmc_mac": bmc_mac,
                        "consecutive_unknowns": 3,
                        "latched": False,
                    })
            else:
                if prev_streak >= 3:
                    emit_event({
                        "kind": "bmc_poll_recovered",
                        "switch": port_state["switch"],
                        "port": port_state["port"],
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
            # physical chassis. Once we've read it successfully, transient
            # IPMI/SSH failures shouldn't clobber it back to notfound.
            # The latch is cleared on link drop (chassis swap signal)
            # and on bmc_mac change (different chassis on same port).
            _set_var(port_state, "chassissn", "found", emit_event)
        else:
            port_state["chassis_sn"] = None
            _set_var(port_state, "chassissn", "notfound", emit_event)

    nodepxe_state = nginx_pxe_seen(
        env.get("nginx_access_log", "/var/log/nginx/access.log"),
        port_state["nic_ip"],
    )
    _set_var(port_state, "nodepxe", nodepxe_state, emit_event)

    ssh_state = ssh_uptime(
        port_state["nic_ip"],
        env.get("host_credentials", []),
    )
    _set_var(port_state, "nodessh", ssh_state, emit_event)

    inv_state = inventory_status(
        env.get("nodes_root", "/export/nodes"),
        port_state["nic_mac"],
        port_state["vars"]["linkstate"]["since"],
    )
    _set_var(port_state, "inventory", inv_state, emit_event)


class PortWorker:
    """Background thread; one per (switch, port). Independent cycle so
    a 20s+ BMC stall on this port can't block any other."""

    def __init__(self, port_state_slot, switch_facts, switch_facts_lock,
                 state_lock, emit_event, env,
                 cycle_secs=CYCLE_SECS, snapshot_dirty_event=None):
        self._slot = port_state_slot
        self._switch_facts = switch_facts
        self._switch_facts_lock = switch_facts_lock
        self._state_lock = state_lock
        self._emit = emit_event
        self._env = env
        self._cycle = cycle_secs
        self._dirty = snapshot_dirty_event
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"port-worker-{port_state_slot['switch']}-{port_state_slot['port']}",
            daemon=True,
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=20.0)

    def _run(self):
        sw = self._slot["switch"]
        while not self._stop.is_set():
            with self._switch_facts_lock:
                # Project per-port view
                pp = self._switch_facts.get((sw, self._slot["port"]))
                # port_worker_one_iter expects {port: {linkstate, macs}}
                pp_view = {self._slot["port"]: pp} if pp else {}
            # Don't hold state_lock here: port_worker_one_iter does slow
            # probes (ipmitool, ssh, ping) inline, and grabbing the lock
            # for the whole iteration starves the HTTP handler /api/v1/state.
            # Each worker only mutates its own slot dict, and individual
            # dict key writes are GIL-atomic — readers (snapshot debouncer,
            # HTTP handler) may see a port mid-update but that's eventually
            # consistent (next cycle re-renders).
            port_worker_one_iter(
                port_state=self._slot,
                switch_facts=pp_view,
                emit_event=self._emit,
                env=self._env,
            )
            if self._dirty:
                self._dirty.set()
            self._stop.wait(self._cycle)


# === 7. HTTP surface ========================================================

import http.server
import socketserver


_DEEPEST_BY_VAR = {v: v for v in STATE_VARS}


def _deepest_state(ps):
    """Return the var name representing the 'deepest reached' good state.

    Walk STATE_VARS in order; the last variable whose value is in the
    success set wins.
    """
    success = {
        "linkstate": {"link"},
        "bmcmac": {"found"}, "bmcip": {"found"}, "bmcping": {"ok"},
        "bmcipmi": {"openbmc", "traditional"}, "bmcpower": {"on"}, "chassissn": {"found"},
        "nodeip": {"found"}, "nodeping": {"ok"}, "nodepxe": {"found"},
        "nodessh": {"ok"}, "inventory": {"found"},
    }
    deepest = "linkstate"
    for v in STATE_VARS:
        if ps["vars"][v]["value"] in success.get(v, set()):
            deepest = v
    return deepest


def _load_events(events_path, archive=False):
    """Read events.jsonl (and rotated backups if archive=True). Returns list
    of raw lines (no JSON parse — the caller filters)."""
    paths = []
    if archive:
        # Walk backwards: events.jsonl.4 (oldest) → .1 → events.jsonl (newest)
        for n in range(EVENTS_BACKUP_COUNT, 0, -1):
            cand = f"{events_path}.{n}"
            if _os_mod.path.exists(cand):
                paths.append(cand)
    paths.append(events_path)
    lines = []
    for p in paths:
        try:
            with open(p) as f:
                lines.extend(line.rstrip("\n") for line in f)
        except FileNotFoundError:
            continue
    return lines


def _render_dashboard_html(rack_name):
    # Built outside the f-string because Python 3.11 rejects backslashes
    # (the escaped `\"` quotes) inside f-string expression substitutions.
    var_cells_js = " +\n        ".join(
        '`<td class="${ps.vars.' + v + '.value}">${ps.vars.' + v + '.value}</td>`'
        for v in STATE_VARS
    )
    th_cells = "".join(f"<th>{v}</th>" for v in STATE_VARS)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>switchportrecon · {rack_name}</title>
<style>
body{{font-family:monospace;margin:1em;}}
table{{border-collapse:collapse;width:100%;}}
th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left;}}
th{{background:#eef;cursor:pointer;}}
td.link,td.found,td.ok,td.on,td.openbmc,td.traditional{{background:#cfc;}}
td.nolink,td.fail,td.notfound,td.off{{background:#fcc;}}
td.unknown{{background:#eee;color:#888;}}
.banner{{padding:8px;background:#eef;border:1px solid #cce;margin-bottom:8px;}}
.banner.stale{{background:#fee;border-color:#fcc;}}
</style></head><body>
<div class="banner" id="banner">switchportrecon · {rack_name} · loading…</div>
<table><thead><tr><th>Port</th><th>OU</th><th>Switch</th>
{th_cells}
<th>Last polled</th></tr></thead><tbody id="rows"></tbody></table>
<script>
async function refresh() {{
  try {{
    const r = await fetch('/api/v1/state');
    const d = await r.json();
    const tbody = document.getElementById('rows');
    tbody.innerHTML = '';
    for (const ps of d.ports) {{
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><a href="/port/${{ps.port}}">${{ps.port}}</a></td>` +
        `<td>${{ps.ou}}</td><td>${{ps.switch}}</td>` +
        {var_cells_js} +
        `<td>${{ps.last_polled || '—'}}</td>`;
      tbody.appendChild(tr);
    }}
    document.getElementById('banner').textContent =
      `switchportrecon · {rack_name} · ${{d.ports.length}} ports · refreshed ${{new Date().toISOString()}}`;
  }} catch(e) {{
    document.getElementById('banner').className = 'banner stale';
    document.getElementById('banner').textContent = 'fetch error: ' + e;
  }}
}}
refresh(); setInterval(refresh, 5000);
</script></body></html>"""


def _render_drilldown_html(ps, rack_name):
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{ps['port']} · switchportrecon</title>
<style>
body{{font-family:monospace;margin:1em;}}
table{{border-collapse:collapse;}}
th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left;}}
.cur{{margin-bottom:1em;}}
</style></head><body>
<h1>{ps['port']} · {ps['ou']} · {ps['switch']} ({rack_name})</h1>
<div class="cur">Last polled: <span id="last">{ps.get('last_polled') or '—'}</span></div>
<h2>Current state</h2>
<table id="cur"><thead><tr><th>Var</th><th>Value</th><th>Since</th></tr></thead>
<tbody id="curbody"></tbody></table>
<h2>Timeline (most recent first)</h2>
<table id="tl"><thead><tr><th>ts</th><th>var</th><th>from → to</th></tr></thead>
<tbody id="tlbody"></tbody></table>
<button id="older">Load older (archive)</button>
<script>
const PORT = '{ps['port']}';
async function refresh() {{
  const r = await fetch('/api/v1/state');
  const d = await r.json();
  const ps = d.ports.find(p => p.port === PORT);
  if (!ps) return;
  const cb = document.getElementById('curbody');
  cb.innerHTML = '';
  // Sort by `since` descending (most recent first); rows with no `since`
  // (vars that have never transitioned) sink to the bottom. ISO 8601
  // timestamps compare lexicographically the same as chronologically.
  const entries = Object.entries(ps.vars).sort(([, a], [, b]) => {{
    if (!a.since && !b.since) return 0;
    if (!a.since) return 1;
    if (!b.since) return -1;
    return b.since.localeCompare(a.since);
  }});
  for (const [v, meta] of entries) {{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${{v}}</td><td>${{meta.value}}</td><td>${{meta.since||'—'}}</td>`;
    cb.appendChild(tr);
  }}
  document.getElementById('last').textContent = ps.last_polled || '—';
}}
async function loadEvents(archive) {{
  const url = `/api/v1/events?port=${{PORT}}&limit=200` + (archive ? '&archive=true' : '');
  const r = await fetch(url);
  const text = await r.text();
  const tl = document.getElementById('tlbody');
  tl.innerHTML = '';
  for (const line of text.trim().split('\\n').reverse()) {{
    if (!line) continue;
    const e = JSON.parse(line);
    if (e.kind !== 'transition') continue;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${{e.ts}}</td><td>${{e.var}}</td><td>${{e.from}} → ${{e.to}}</td>`;
    tl.appendChild(tr);
  }}
}}
document.getElementById('older').onclick = () => loadEvents(true);
refresh(); loadEvents(false); setInterval(refresh, 5000);
</script></body></html>"""


def _make_handler(port_state, state_lock, events_path, rack_name):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            pass

        def _send_json(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, code, text, ctype="text/plain"):
            body = text.encode() if isinstance(text, str) else text
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send_text(200, _render_dashboard_html(rack_name),
                                       "text/html")
            if path.startswith("/port/"):
                want = path[len("/port/"):]
                with state_lock:
                    matches = [v for (sw, p), v in port_state.items() if p == want]
                if not matches:
                    return self._send_json(404, {"error": "not found"})
                return self._send_text(
                    200, _render_drilldown_html(matches[0], rack_name), "text/html"
                )
            if path == "/api/v1/healthz":
                return self._send_text(200, "ok\n")
            if path == "/api/v1/ports":
                with state_lock:
                    keys = sorted({p for (_, p) in port_state.keys()})
                return self._send_json(200, keys)
            if path.startswith("/api/v1/port/"):
                want = path[len("/api/v1/port/"):]
                with state_lock:
                    matches = [v for (sw, p), v in port_state.items() if p == want]
                if not matches:
                    return self._send_json(404, {"state": "missing"})
                ps = matches[0]
                snap = render_status_snapshot(ps, _deepest_state(ps))
                snap["switch"] = ps["switch"]
                return self._send_json(200, snap)
            if path == "/api/v1/rackous":
                with state_lock:
                    geo = [
                        {"ou": ps["ou"], "port": ps["port"]}
                        for (_, _), ps in sorted(
                            port_state.items(), key=lambda kv: kv[1]["index"]
                        )
                    ]
                return self._send_json(200, {
                    "maxPower": 1200,
                    "geometry": geo,
                    "rackName": rack_name,
                })
            if path == "/api/v1/state":
                with state_lock:
                    payload = {
                        "snapshotted_at": _ts_now(),
                        "ports": [
                            {**ps, "snapshot": render_status_snapshot(
                                ps, _deepest_state(ps))}
                            for (_, _), ps in sorted(
                                port_state.items(), key=lambda kv: kv[1]["index"]
                            )
                        ],
                    }
                return self._send_json(200, payload)
            if path == "/api/v1/events":
                if not events_path:
                    return self._send_json(200, [])
                qs = self.path.split("?", 1)
                params = {}
                if len(qs) > 1:
                    for kv in qs[1].split("&"):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            params[k] = v
                want_port = params.get("port")
                want_switch = params.get("switch")
                want_since = params.get("since")
                limit = int(params.get("limit", "200"))
                limit = min(limit, 5000)
                archive = params.get("archive") == "true"
                lines = _load_events(events_path, archive=archive)
                # Filter
                out = []
                for raw in lines:
                    try:
                        e = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if want_port and e.get("port") != want_port:
                        continue
                    if want_switch and e.get("switch") != want_switch:
                        continue
                    if want_since and e.get("ts", "") < want_since:
                        continue
                    out.append(raw)
                # Tail to limit
                out = out[-limit:]
                body = "\n".join(out) + ("\n" if out else "")
                return self._send_text(200, body, "application/x-ndjson")
            return self._send_json(404, {"error": "not found"})

    return _Handler


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_http_server(port_state, state_lock, events_path, rack_name,
                      bind=("0.0.0.0", DASHBOARD_PORT)):
    """Start the daemon's HTTP server on a background thread.

    Returns (server, url).
    """
    handler_cls = _make_handler(port_state, state_lock, events_path, rack_name)
    server = _ThreadedHTTPServer(bind, handler_cls)
    threading.Thread(
        target=server.serve_forever, name="http-server", daemon=True
    ).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


# === 8. Main ================================================================

import signal


def _check_vip_holder():
    """Returns 0 if local node holds the bang mgmt VIP, else 1.

    Mirrors reaper-leased's pattern. The address comparison reads
    `ip addr show` and looks for the configured mgmt-VIP literal.
    """
    # Read our configured VIP from /etc/flax/site.env if present
    vip = None
    try:
        with open("/etc/flax/site.env") as f:
            for line in f:
                if line.startswith("MGMT_VIP="):
                    vip = line.split("=", 1)[1].strip().strip('"')
                    break
    except FileNotFoundError:
        pass
    if not vip:
        # Solo-bang mode: no VIP gate, run anyway
        return 0
    cp = subprocess.run(
        ["ip", "addr", "show"], stdout=subprocess.PIPE, timeout=5
    )
    return 0 if vip in cp.stdout.decode("utf-8", errors="ignore") else 1


def main(argv=None):
    # Ensure shared SSH known_hosts dir exists. Idempotent; matches the
    # reaper-leased side so either daemon (running first or alone) creates it.
    # `os` is imported locally per the existing pattern in this file
    # (module-level uses `_os_mod` alias).
    import os
    try:
        os.makedirs(os.path.dirname(SSH_KNOWN_HOSTS), mode=0o700, exist_ok=True)
    except OSError:
        pass  # tolerate read-only FS — ssh will recreate the file lazily anyway
    parser = argparse.ArgumentParser(description="Switch port recon daemon")
    parser.add_argument("--check-vip-holder", action="store_true")
    parser.add_argument("--geometry", default=GEOMETRY_PATH)
    parser.add_argument("--switches", default=SWITCHES_PATH)
    parser.add_argument("--credentials", default=CREDENTIALS_PATH)
    parser.add_argument("--bmc-credentials", default=BMC_CREDENTIALS_PATH)
    parser.add_argument("--host-credentials", default=HOST_CREDENTIALS_PATH)
    parser.add_argument("--state-root", default=STATE_ROOT)
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--bind-port", type=int, default=DASHBOARD_PORT)
    parser.add_argument("--cycle-secs", type=float, default=CYCLE_SECS)
    args = parser.parse_args(argv)

    if args.check_vip_holder:
        return _check_vip_holder()

    snap_path = _os_mod.path.join(args.state_root, "state.json")
    events_path = _os_mod.path.join(args.state_root, "events.jsonl")
    _os_mod.makedirs(args.state_root, exist_ok=True)

    events = EventsWriter(events_path)
    emit = events.emit

    # One-shot janitor: reap any intentional-flap sentinels left by a
    # crashed reaper-leased run. Their `until` deadlines may have
    # passed long ago; without this, port_worker_one_iter would still
    # lazy-unlink them on next access, but only AFTER the consumer's
    # first check found them stale — a clean startup leaves nothing.
    try:
        sweep_stale_flap_sentinels()
    except Exception as e:
        emit({"kind": "flap_sentinel_sweep_failed", "error": repr(e)})

    switches = load_switches(args.switches)
    credentials = load_credentials(args.credentials)
    bmc_credentials = load_bmc_credentials(args.bmc_credentials)
    host_credentials = load_host_credentials(args.host_credentials)
    geometry = load_geometry(args.geometry,
                             default_switch_name=switches[0]["name"])

    port_state, _ = load_or_init_port_state(snap_path, geometry, emit)

    state_lock = threading.RLock()
    facts_lock = threading.RLock()
    facts = {}
    snapshot_dirty = threading.Event()

    debouncer = SnapshotDebouncer(
        snap_path, port_state, state_lock,
        events_writer=events,
    )
    # Plumb the same dirty event so port workers wake the debouncer
    debouncer._dirty = snapshot_dirty
    debouncer.start()

    # Read /etc/hostname for rack name (just like the api container)
    try:
        rack_name = open("/etc/hostname").read().strip().rsplit("-", 1)[-1]
    except FileNotFoundError:
        rack_name = "unknown"

    env = {
        "leases_path": "/var/lib/misc/dnsmasq.leases",
        "dhcp_hosts_dir": "/etc/dnsmasq.dhcp-hosts",  # static reservations dir; fallback when MAC isn't in active leases
        "credentials": credentials,             # openbmc path uses obmcuser/obmcpass
        "bmc_credentials": bmc_credentials,     # traditional IPMI cred-walk: [{bmcuser, bmcpass}, ...]
        "host_credentials": host_credentials,   # ssh node-uptime cred-walk: [{user, pass}, ...]
        "nginx_access_log": "/var/log/nginx/access.log",
        "nodes_root": "/export/nodes",
    }

    # Switch fetchers
    fetchers = []
    for sw in switches:
        # Skip switches whose driver this daemon can't build (e.g. the cumulus
        # stub) rather than crashing the whole daemon. The shared
        # /etc/flax/switches.json now includes the turtle (cumulus) for the
        # flax-* services; switchportrecond just ignores what it can't drive.
        try:
            drv = make_driver(sw, credentials)
        except Exception as e:  # noqa: BLE001 -- any driver/cred failure -> skip
            emit({"kind": "switch_skipped", "switch": sw["name"],
                  "driver": sw.get("driver"), "detail": str(e)})
            continue
        f = SwitchFetcher(
            switch_name=sw["name"], driver=drv,
            switch_facts=facts, switch_facts_lock=facts_lock,
            emit_event=emit, cycle_secs=args.cycle_secs,
        )
        f.start()
        fetchers.append(f)

    # Port workers
    workers = []
    for key, slot in port_state.items():
        w = PortWorker(
            port_state_slot=slot,
            switch_facts=facts, switch_facts_lock=facts_lock,
            state_lock=state_lock,
            emit_event=emit, env=env, cycle_secs=args.cycle_secs,
            snapshot_dirty_event=snapshot_dirty,
        )
        w.start()
        workers.append(w)

    # HTTP
    server, _ = start_http_server(
        port_state=port_state, state_lock=state_lock,
        events_path=events_path, rack_name=rack_name,
        bind=(args.bind_host, args.bind_port),
    )

    # Signal handling
    stopping = threading.Event()

    def handle_term(signum, frame):
        stopping.set()

    def handle_hup(signum, frame):
        # Reload geometry/switches; for v1, just emit an event.
        # Full live-reload is a follow-up.
        emit({"kind": "reload", "reason": "SIGHUP",
              "added": [], "removed": []})

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)
    signal.signal(signal.SIGHUP, handle_hup)

    # Background writer: snapshot _LLDP_BY_SWITCH to LLDP_CACHE_PATH every
    # cycle so reaper-leased can corroborate enrollment MACs. Same cadence
    # as the fetchers — there is no benefit to writing faster than fetches
    # populate the dict. Daemon thread; shutdown is driven by `stopping`.
    def _lldp_cache_loop():
        while not stopping.is_set():
            with facts_lock:
                facts_per_switch = dict(_LLDP_BY_SWITCH)
            try:
                write_lldp_cache(facts_per_switch)
            except Exception as e:
                emit({"kind": "lldp_cache_write_failed", "error": repr(e)})
            stopping.wait(args.cycle_secs)

    threading.Thread(target=_lldp_cache_loop,
                     name="lldp-cache-writer", daemon=True).start()

    stopping.wait()

    # Graceful shutdown
    for w in workers:
        w.stop()
    for f in fetchers:
        f.stop()
    debouncer.stop()
    server.shutdown()
    events.flush_and_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
