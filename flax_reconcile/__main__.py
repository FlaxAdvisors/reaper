"""flax-reconcile entrypoint. Mirrors flax_discover.__main__.

  Periodic sweep every config["sweep_interval_secs"] (level-triggered backstop).
  Debounced cycle on every LISTEN ping (lease_events / kea_hosts_change /
  desired_port / reconcile_requests).
  Healthz on --healthz-port (default 10996).

Credentials wiring:
  --switches     /etc/flax/switches.json         switch driver + eAPI/ssh creds
  --credentials  /etc/flax/credentials.json      eosuser/eospass + obmcuser/obmcpass
  --credentials-host /etc/flax/credentials-host.json  host-ssh kick creds

  credentials.json carries BOTH the switch transport creds (eosuser/eospass,
  cisco_user/cisco_pass) AND the openbmc ssh creds (obmcuser/obmcpass), matching
  scripts/reaper_leased.py lines 1469/1485.  credentials-bmc.json is the IPMI
  cred list used only by the IPMI/observe path -- flax-reconcile does NOT use it.

VLAN parents:
  --vlans /etc/flax/vlans.json    list of {vid, parent, ...}; parent is the
  host interface name for the BMC-LL kick rung (e.g. "eth0.17").

No-steer list:
  --no-steer /etc/flax/no-steer-ports.json    [{switch, port}] hard-excluded
  from VLAN steering (independent of classify's own guard).

py3.11 note: _build_conninfo uses string concatenation (dsn_k + "=" + v), not
f-strings, to stay safe on bang-fiesta which runs Python 3.11 (no backslashes
inside f-string expressions per feedback_python311_fstring_no_backslashes).
"""
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time

from . import role_caps, selfheal
from .actions import write_ack
from .bmc_reset import load_redfish_creds
from .config import load_config
from .cycle import Reconciler
from .db import build_pool
from .drivers import load_switches
from .healthz import HealthState
from .healthz import serve as serve_healthz
from .listen import Debouncer
from .listen import listen_loop
from .steer import load_no_steer

log = logging.getLogger("flax-reconcile")


def _ack_action(summary: dict) -> str:
    """applied if the cycle steered, enqueued, or kicked anything, else noop."""
    if (summary.get("steered") or summary.get("enqueued")
            or summary.get("kicked")):
        return "applied"
    return "noop"


def _ack_cycle(pool, generation, summary: dict) -> None:
    """Success-path consumer_acks write. Wrapped in its own try/except so a
    ledger write failure can never crash the cycle."""
    try:
        write_ack(pool, "flax-reconcile", "desired_port", generation,
                  _ack_action(summary))
    except Exception:
        log.exception("write_ack (success) failed; continuing")


def _ack_failed(pool, generation, exc) -> None:
    """Except-path consumer_acks write. detail truncated to 200 chars; these
    services' exceptions carry no credentials."""
    write_ack(pool, "flax-reconcile", "desired_port", generation, "failed",
              detail=str(exc)[:200])


def _build_conninfo() -> str:
    """Resolve from env vars (PGHOST, PGUSER, etc.) -- mirrors
    flax_discover._build_conninfo. psycopg DSN keys are host=, port=, user=,
    password=, dbname= (NOT pghost=); map explicitly.

    Uses string concatenation (not f-strings) for py3.11 compatibility:
    bang-fiesta runs Python 3.11 which forbids backslashes inside f-string
    expressions (feedback_python311_fstring_no_backslashes).
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
            raise RuntimeError("required env var " + env_k + " is not set")
        parts.append(dsn_k + "=" + v)
    parts.append("application_name=flax-reconcile")
    return " ".join(parts)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(prog="flax-reconcile")
    p.add_argument("--config", default="/etc/flax/reconcile.json",
                   help="Path to reconcile.json tunables (missing file -> built-in defaults)")
    p.add_argument("--switches", default="/etc/flax/switches.json",
                   help="Path to switches.json (switch list + driver + host)")
    p.add_argument("--credentials", default="/etc/flax/credentials.json",
                   help="Path to credentials.json (eosuser/eospass + obmcuser/obmcpass)")
    p.add_argument("--credentials-host", default="/etc/flax/credentials-host.json",
                   help="Path to credentials-host.json (host-ssh kick creds)")
    p.add_argument("--credentials-redfish",
                   default="/etc/flax/credentials-redfish.json",
                   help="Path to credentials-redfish.json (list of {bmcuser, "
                        "bmcpass}); missing/empty file -> [] (operator BMC-reset "
                        "then fails closed, never crashes the cycle)")
    p.add_argument("--vlans", default="/etc/flax/vlans.json",
                   help="Path to vlans.json (list of {vid, parent, ...})")
    p.add_argument("--no-steer", default="/etc/flax/no-steer-ports.json",
                   help="Path to no-steer-ports.json (hard-excluded ports)")
    p.add_argument("--healthz-port", type=int, default=10996)
    p.add_argument("--healthz-stale-secs", type=float, default=1800.0)
    return p.parse_args(argv)


def _load_vlan_parents(vlans_path: str) -> dict:
    """Parse vlans.json -> {vid: parent_iface}.

    vlans.json is a list of {vid, parent, ...} dicts (same schema that
    reaper_leased.load_vlans reads). Only entries with a "parent" key are
    included -- management/untagged VLANs typically have no parent iface.
    Missing file -> empty dict (no BMC-LL iface overrides). Malformed -> fatal.
    """
    try:
        with open(vlans_path) as f:
            entries = json.load(f)
    except FileNotFoundError:
        log.info("no %s; vlan_parents will be empty", vlans_path)
        return {}
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError("malformed vlans file " + vlans_path + ": " + str(e)) from e
    return {entry["vid"]: entry["parent"] for entry in entries if "parent" in entry}


def _load_eligible_sources(pool):
    """Registry capability lookup (Task 1, spec 2026-07-03 spine migration):
    delegates to role_caps.read_reconcile_eligible_sources, which reads the
    `roles` table once and returns the frozenset of reconcile-eligible
    user_context.source values, or None (registry empty/unpublished) to
    signal the legacy `source <> 'post'` fallback in db.read_reservations.

    Thin wrapper kept for the same reason as _load_vlan_parents/load_no_steer/
    load_redfish_creds: a single named startup-read call site the entrypoint
    reads ONCE (mirrors those patterns) and tests can monkeypatch directly.
    """
    return role_caps.read_reconcile_eligible_sources(pool)


def main(argv=None):
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    cfg = load_config(args.config)
    conninfo = _build_conninfo()
    pool = build_pool(conninfo)
    health = HealthState(stale_secs=args.healthz_stale_secs)

    # Load switch drivers + credentials (creds wiring correction: use
    # args.credentials not args.credentials_bmc -- credentials.json holds both
    # switch transport creds AND obmcuser/obmcpass for the BMC-LL kick rung).
    switches, obmc_user, obmc_pass, host_creds = load_switches(
        args.switches, args.credentials, args.credentials_host)

    # vlan_parents: vid -> parent iface for the BMC-LL kick rung.
    vlan_parents = _load_vlan_parents(args.vlans)

    # no_steer: hard-excluded (switch, port) pairs; independent of classify's guard.
    no_steer = load_no_steer(args.no_steer)

    # redfish_creds: list of {bmcuser, bmcpass} for the operator BMC-reset path.
    # Missing/empty file -> [] (tolerant: a docker bind-mount of a missing host
    # file yields an empty dir; load_redfish_creds swallows that to []).
    redfish_creds = load_redfish_creds(args.credentials_redfish)

    # eligible_sources: registry capability lookup (Task 1), read ONCE at
    # startup like every other config source above. frozenset -> the
    # registry-driven read_reservations filter; None -> the roles table is
    # empty/unpublished and read_reservations falls back to the legacy
    # `source <> 'post'` literal (deploy-order safety; role_caps already
    # logged the warning).
    eligible_sources = _load_eligible_sources(pool)

    recon = Reconciler(
        switches=switches,
        config=cfg,
        obmc_user=obmc_user,
        obmc_pass=obmc_pass,
        host_creds=host_creds,
        no_steer=no_steer,
        vlan_parents=vlan_parents,
        redfish_creds=redfish_creds,
        eligible_sources=eligible_sources,
    )

    # Monotonic per-process cycle counter used as the consumer_acks generation.
    # read_desired_ports does not return desired_port.generation and the plan
    # forbids adding a DB query just for it; generation is informational on the
    # dashboard (it gates on freshness + action), so the counter is the
    # documented fallback. GREATEST in write_ack keeps the row monotonic.
    gen_counter = [0]

    def _do_cycle():
        gen_counter[0] += 1
        try:
            summary = recon.run_one_cycle(pool)
            log.info("cycle steered=%d refused=%d enqueued=%d kicked=%d mismatches=%d",
                     summary["steered"], summary["refused"], summary["enqueued"],
                     summary["kicked"], summary["mismatches"])
            health.record_cycle_done(**summary)
            _ack_cycle(pool, gen_counter[0], summary)
        except Exception as e:
            log.exception("cycle failed")
            _ack_failed(pool, gen_counter[0], e)

    # Startup self-heal: un-strand any port a previously-killed flap left
    # admin-down (process death between flap()'s shutdown and no-shutdown
    # batches). Runs ONCE, before the sweep loop, so a restart never inherits a
    # stranded port. Best-effort -- never blocks startup on a switch error.
    try:
        n = selfheal.recover_stranded_ports(pool, switches, no_steer=no_steer)
        log.info("startup self-heal: recovered %d stranded admin-down port(s)", n)
    except Exception:
        log.exception("startup self-heal failed; continuing")

    debouncer = Debouncer(target=_do_cycle, debounce_secs=cfg["debounce_secs"])
    debouncer.start()

    threading.Thread(
        target=serve_healthz, args=(health, args.healthz_port),
        daemon=True).start()

    threading.Thread(
        target=_listen_with_reconnect, args=(conninfo, debouncer),
        daemon=True).start()

    # SIGTERM (systemctl stop) handler: set a stop flag so the sweep loop exits
    # cleanly between cycles, and -- if a flap is in-flight (a flap_pending
    # marker is set) -- complete the no-shutdown for the pending port(s) before
    # exiting so a `systemctl stop` mid-flap can never strand a port. The marker
    # is only ever set between a flap's shutdown and its no-shutdown (cycle.py /
    # kick.py mark before, clear after), so completing every pending marker here
    # is exactly "finish the in-flight no-shutdown."
    stop = threading.Event()

    def _on_sigterm(signum, frame):
        log.info("SIGTERM received; completing any in-flight flap then exiting")
        try:
            n = selfheal.recover_stranded_ports(pool, switches, no_steer=no_steer)
            log.info("SIGTERM self-heal: completed %d in-flight flap(s)", n)
        except Exception:
            log.exception("SIGTERM self-heal failed; exiting anyway")
        stop.set()

    signal.signal(signal.SIGTERM, _on_sigterm)

    # Periodic sweep (LISTEN-independent level-triggered backstop). The SIGTERM
    # handler can fire between cycles (the flag is checked here) OR during a
    # cycle -- in the latter case the current cycle's flap finishes normally
    # (mark/clear is synchronous within _do_cycle) and the handler's own
    # recover pass mops up anything genuinely mid-flap before the flag is read.
    while not stop.is_set():
        _do_cycle()
        if stop.wait(timeout=cfg["sweep_interval_secs"]):
            break
    log.info("flax-reconcile exiting cleanly")


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
