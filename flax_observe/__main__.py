"""Daemon entrypoint: python -m flax_observe

Boots SwitchFactsListener + N PortWorkers + /healthz HTTP server.

CLI flags:
  --check-vip-holder   Exit 0 iff this bang holds MGMT_VIP per /etc/flax/site.env
                       (system unit's ExecStartPre uses a bash one-liner; this
                        flag is for operator ad-hoc use inside the container)
"""
import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from . import enroll
from .geometry import load_geometry
from .role_caps import read_observe_eligible_switches
from .healthz import WorkerStatus, build_handler
from .persistence import read_active_sentinels
# Imported at module top (not inside main) so a test can monkeypatch
# flax_observe.__main__.write_ack to capture the supervisor ledger write.
from .persistence import read_prior_observe_state
from .persistence import write_ack
from .port_worker import PortWorker, make_env, _internal_to_arista
from .switch_facts import SwitchFactsCache, SwitchFactsListener


log = logging.getLogger("flax-observe")


def _observe_switches(pool) -> list:
    """Dynamic-enrollment scope: the switches marked observe-capable in the
    published role registry (mirrors flax_reconcile's registry-first sourcing).
    Replaces the hand-maintained /etc/flax/rabbit-geometry.json file. A None
    return from the reader (registry empty/unreadable) collapses to [] -> no
    dynamic enrollment -> today's static behaviour, the safe deploy-order
    fallback. Wrapped as a module-level seam so tests can monkeypatch it."""
    return list(read_observe_eligible_switches(pool) or ())


def _ack_observe(pool, generation) -> None:
    """Success-path consumer_acks write for flax-observe, emitted ONCE per
    supervisor cycle (NOT per PortWorker -- that would put ~58 writers on one
    row). Wrapped in its own try/except so a ledger write failure can NEVER
    crash the supervisor loop -- the ack is best-effort dashboard freshness."""
    try:
        write_ack(pool, "flax-observe", "switch_facts", generation, "applied")
    except Exception:
        log.exception("write_ack (success) failed; continuing")


def _ack_observe_failed(pool, generation, exc) -> None:
    """Except-path consumer_acks write: mark flax-observe unhealthy. detail is
    truncated to 200 chars; observe's exceptions carry no credentials."""
    write_ack(pool, "flax-observe", "switch_facts", generation, "failed",
              detail=str(exc)[:200])


def reconcile_workers(desired, running, static_keys, make, statuses):
    """Reconcile the live worker set against `desired`.

    desired: {(switch, port) -> entry} where entry has switch/port/ou.
    running: {(switch, port) -> worker} mutated in place.
    static_keys: keys that must NEVER be stopped (Triage + turtle).
    make: factory(entry) -> worker; the supervisor calls .start() on it.
    statuses: WorkerStatus dict; a removed worker's status is popped so
              /healthz doesn't report a stale stopped worker.

    Pure-ish (no threads/network/DB of its own): all side effects go through
    the injected `make` factory and the worker's .start()/.stop()/.join().
    Used by both the initial build and the supervisor loop.
    """
    for key, entry in desired.items():
        if key not in running:
            w = make(entry)
            running[key] = w
            w.start()
            log.info("enroll: added worker %s/%s", entry["switch"], entry["port"])
    for key in list(running):
        if key not in desired and key not in static_keys:
            w = running.pop(key)
            w.stop()
            w.join(timeout=5)
            statuses.pop(key, None)
            log.info("enroll: removed worker %s/%s", key[0], key[1])


def make_worker(switch, port, ou, *, cache, env, statuses, refresh_sentinels,
                cycle_secs, prior=None):
    """Build a fully-wired PortWorker: register its WorkerStatus in `statuses`,
    wrap _cycle_once to refresh sentinels + update status, and return it
    (NOT started). Identical wiring to the original startup loop."""
    w = PortWorker(switch, port, ou, cache, env, cycle_secs=cycle_secs,
                   prior=prior)
    statuses[(switch, port)] = WorkerStatus(switch=switch, port=port,
                                            last_cycle=None, last_error=None)
    original = w._cycle_once

    def make_wrapped(orig, status_ref):
        def wrapped():
            refresh_sentinels()
            orig()
            status_ref.last_cycle = time.time()
            status_ref.last_error = None
        return wrapped

    w._cycle_once = make_wrapped(original, statuses[(switch, port)])
    return w


def make_sentinel_check(sentinel_cache: dict):
    """Build the intentional_flap_active(sw, port) predicate.

    PortWorker calls the returned predicate with the geometry port in internal
    short form (et6b1), but flax-reconcile writes intentional_flap sentinels in
    Arista canonical form (Ethernet6/1) -- matching switch_facts/desired_port.
    Canonicalize the lookup key so a steered/kicked port's freeze is actually
    seen (spec §9); otherwise observe treats the intentional flap as a real
    transition. sentinel_cache is the mutable {"set": <frozen (sw, port) set>}
    refreshed each cycle, so the predicate always reads the latest snapshot.
    """
    def _active(sw, port):
        return (sw, _internal_to_arista(port)) in sentinel_cache["set"]
    return _active


def _read_site_env(path: str = "/etc/flax/site.env") -> dict:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _ip_held_locally(ip: str) -> bool:
    if not ip:
        return False
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr"], timeout=2).decode()
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return any(line.split()[3].split("/")[0] == ip
               for line in out.splitlines() if len(line.split()) >= 4)


def check_vip_holder() -> int:
    env = _read_site_env()
    vip = env.get("MGMT_VIP", "")
    if not vip:
        log.error("MGMT_VIP not set in /etc/flax/site.env -- refusing to start")
        return 1
    return 0 if _ip_held_locally(vip) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flax-observe")
    parser.add_argument("--check-vip-holder", action="store_true")
    parser.add_argument("--geometry", default="/etc/flax/geometry.json")
    parser.add_argument("--turtle-geometry", default="/etc/flax/turtle-geometry.json")
    parser.add_argument("--no-steer", default="/etc/flax/no-steer-ports.json")
    parser.add_argument("--enroll-resync-secs", type=int, default=30)
    parser.add_argument("--credentials", default="/etc/flax/credentials.json")
    parser.add_argument("--bmc-credentials", default="/etc/flax/credentials-bmc.json")
    parser.add_argument("--redfish-credentials", default="/etc/flax/credentials-redfish.json")
    parser.add_argument("--host-credentials", default="/etc/flax/credentials-host.json")
    parser.add_argument("--vlans", default="/etc/flax/vlans.json",
                        help="vlans.json (list of {vid, parent, ...}); "
                             "vid->parent iface map for IPv6-LL BMC reach")
    parser.add_argument("--macmath-dir", default="/etc/flax/macmath",
                        help="dir of <vid>.json MAC-math configs; per-vid "
                             "BMC<->NIC classification override (absent -> "
                             "legacy +/-2 pairing everywhere)")
    parser.add_argument("--default-switch", default="rabbit-lorax",
                        help="default switch for geometry entries with no 'switch' field")
    parser.add_argument("--cycle-secs", type=float, default=10.0)
    parser.add_argument("--healthz-port", type=int, default=10993)
    parser.add_argument("--healthz-stale-secs", type=float, default=60.0)
    parser.add_argument("--sentinel-grace-secs", type=int, default=30,
                        help="extra seconds beyond hold_seconds before a "
                             "Postgres intentional_flap sentinel expires")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.check_vip_holder:
        return check_vip_holder()

    # Load configs
    geometry = load_geometry(args.geometry, default_switch_name=args.default_switch)

    # Load turtle geometry (optional — absent or dir path → empty list)
    turtle_entries: list[dict] = []
    tg_path = args.turtle_geometry
    if os.path.exists(tg_path) and not os.path.isdir(tg_path):
        try:
            turtle_entries = load_geometry(tg_path)
        except Exception:
            log.exception("turtle-geometry load failed (%s); skipping turtle ports", tg_path)
    else:
        log.info("turtle-geometry not loaded: path absent or is a directory (%s)", tg_path)

    if turtle_entries:
        log.info("turtle-geometry: adding %d turtle port(s) from %s", len(turtle_entries), tg_path)
    geometry = geometry + turtle_entries
    log.info("total geometry: %d port(s) (%d rabbit/other + %d turtle)",
             len(geometry), len(geometry) - len(turtle_entries), len(turtle_entries))
    with open(args.credentials) as f:
        credentials = json.load(f)
    with open(args.bmc_credentials) as f:
        bmc_credentials = json.load(f)
    with open(args.host_credentials) as f:
        host_credentials = json.load(f)
    # Redfish creds (rfuser/rfpass -> normalized {bmcuser,bmcpass}) for the
    # Redfish BMC identification path. Best-effort: a missing/malformed file
    # yields [] (identification still works via the unauth service root; only
    # the authed product_name read is skipped), so this must NOT hard-fail
    # startup the way the required cred files above do.
    from .ipmi import _load_redfish_credentials
    redfish_credentials = _load_redfish_credentials(args.redfish_credentials)
    log.info("redfish creds: loaded %d entr(ies) from %s",
             len(redfish_credentials), args.redfish_credentials)

    # Switch facts cache + LISTEN consumer
    cache = SwitchFactsCache()
    listener = SwitchFactsListener(cache)
    listener.start()

    # Wait briefly for cache to prime so port workers don't start with empty data
    time.sleep(2)

    # vid -> bang parent iface, for IPv6 link-local BMC reach (probe the
    # OpenBMC at fe80::EUI64%<parent>.<vid> before any IPv4 lease).
    from .port_worker import load_vlan_parents
    vlan_parents = load_vlan_parents(args.vlans)

    # vid -> macmath config, for per-vid BMC<->NIC classification (Task 4).
    # observe_state.nic_mac drives the flax-classify host reservation, so a
    # wedge/SONiC mgmt mac on a distinct_oui vid must classify the same way
    # the switch-sense publisher already does. Absent dir -> {} -> legacy
    # pairing everywhere.
    from flax_switch_sense.macmath import load_macmath_dir
    macmath_by_vid = load_macmath_dir(args.macmath_dir)
    log.info("macmath: loaded %d per-vid config(s) from %s",
             len(macmath_by_vid), args.macmath_dir)

    # Build env + per-port workers
    env = make_env(geometry, credentials, bmc_credentials, host_credentials,
                   vlan_parents=vlan_parents,
                   macmath_by_vid=macmath_by_vid,
                   redfish_credentials=redfish_credentials)

    # Replace the filesystem intentional_flap_active sentinel check with a
    # Postgres-backed one.  A per-cycle-refreshed set is stored in a dict
    # (mutable container) so the lambda always reads the latest snapshot.
    # The filesystem function stays in state_machine.py for back-compat but
    # is no longer the wired path.
    from .db import get_pool as _get_pool
    _sentinel_cache: dict = {"set": set()}

    def _refresh_sentinels() -> None:
        try:
            _sentinel_cache["set"] = read_active_sentinels(
                _get_pool(), grace_secs=args.sentinel_grace_secs)
        except Exception:
            log.exception("sentinel refresh failed; keeping previous set")

    env.intentional_flap_active = make_sentinel_check(_sentinel_cache)

    statuses: dict[tuple[str, str], WorkerStatus] = {}

    # Lab-enrollment scope: the observe-capable switch list (from the role
    # registry) + no-steer drive the dynamic access-port set. Empty/unreadable
    # registry -> [] -> empty dynamic set -> static behavior (today's), so a
    # site whose registry hasn't published yet is unaffected.
    enroll_resync_secs = args.enroll_resync_secs
    rabbit_switches = _observe_switches(_get_pool())
    no_steer = enroll.load_no_steer(args.no_steer)
    log.info("enroll scope: %d rabbit switch(es) %s, %d no-steer port(s)",
             len(rabbit_switches), rabbit_switches, len(no_steer))

    # Static keys (Triage geometry + turtle) are NEVER removed by the supervisor,
    # even if a port momentarily drops out of switch_facts access. They keep
    # their geometry ou; dynamic access ports use ou="".
    static_entries = {(e["switch"], e["port"]): e for e in geometry}
    static_keys = set(static_entries)

    def desired_entries():
        out = dict(static_entries)  # (switch,port)->entry
        for (sw, port) in enroll.dynamic_access(cache, rabbit_switches, no_steer):
            out.setdefault((sw, port), {"switch": sw, "port": port, "ou": ""})
        return out

    # Boot hydration: one batch read of observe_state so each PortWorker can
    # restore its prior inventory latch instead of blanking on restart.
    try:
        prior_rows = read_prior_observe_state(_get_pool())
        log.info("boot hydration: loaded %d prior observe_state row(s)",
                 len(prior_rows))
    except Exception as e:
        log.warning("boot hydration read failed (%s); starting cold", e)
        prior_rows = {}

    def make(entry):
        return make_worker(
            entry["switch"], entry["port"], entry["ou"],
            cache=cache, env=env, statuses=statuses,
            refresh_sentinels=_refresh_sentinels,
            cycle_secs=args.cycle_secs,
            prior=prior_rows.get((entry["switch"], entry["port"])),
        )

    running: dict[tuple[str, str], PortWorker] = {}

    # HTTP /healthz
    handler_cls = build_handler(statuses, max_stale_secs=args.healthz_stale_secs)
    server = ThreadingHTTPServer(("0.0.0.0", args.healthz_port), handler_cls)
    http_thread = threading.Thread(target=server.serve_forever,
                                    name="healthz", daemon=True)
    http_thread.start()

    # Signal handling
    stop_event = threading.Event()
    def on_signal(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        stop_event.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)

    # Initial build: start all desired workers (static + dynamic), then supervise.
    reconcile_workers(desired_entries(), running, static_keys, make, statuses)
    log.info("flax-observe up; %d port worker(s) (%d static), /healthz on :%d",
             len(running), len(static_keys), args.healthz_port)

    # Supervisor loop: re-resync the worker set against the live access ports.
    # Ack the consumer_acks ledger ONCE per iteration here (the central tick),
    # NOT inside PortWorker._cycle_once -- ~58 port workers writing the single
    # (flax-observe, switch_facts) row would be a hotspot.
    #
    # generation: a monotonic per-process counter, the same pattern Task 3 used
    # for the central services. observe's per-port writes already bump
    # observe_state.generation; the dashboard gates on freshness + action, so a
    # supervisor-tick counter is the documented fallback (no extra DB query).
    # GREATEST in write_ack keeps the row monotonic.
    gen_counter = [0]
    while not stop_event.wait(timeout=enroll_resync_secs):
        gen_counter[0] += 1
        try:
            # Re-read the observe-eligible switch set from the registry each
            # resync so a LATE post.json publish (capabilities.observe) self-
            # heals without an observe restart -- the deploy rolls observe then
            # classify, so the first read can predate the republish. Only adopt
            # a SUCCESSFUL read (frozenset, possibly empty); None = unreadable/
            # unpublished -> keep the current scope (no transient flap).
            fresh = read_observe_eligible_switches(_get_pool())
            if fresh is not None and list(fresh) != rabbit_switches:
                rabbit_switches = list(fresh)
                log.info("enroll scope updated from registry: %d switch(es) %s",
                         len(rabbit_switches), rabbit_switches)
            reconcile_workers(desired_entries(), running, static_keys, make, statuses)
            _ack_observe(_get_pool(), gen_counter[0])
        except Exception as e:
            log.exception("supervisor cycle failed")
            # Guard ONLY the best-effort failed-ack so it can never introduce a
            # new crash path; the original supervisor exception is already
            # caught+logged here and the loop continues, exactly as before.
            try:
                _ack_observe_failed(_get_pool(), gen_counter[0], e)
            except Exception:
                log.exception("write_ack (failed) failed; continuing")

    for w in running.values():
        w.stop()
    listener.stop()
    server.shutdown()
    for w in running.values():
        w.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
