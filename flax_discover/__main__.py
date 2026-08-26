# flax_discover/__main__.py
"""flax-discover entrypoint.

  Cycle on a timer (--cycle-secs, default 30).
  Cycle on every debounced LISTEN ping (observe_state + lease_events).
  Healthz on --healthz-port (default 10995).
"""
import argparse
import logging
import os
import sys
import threading
import time

from .cycle import Discoverer
from .db import build_pool
from .healthz import HealthState, serve as serve_healthz
from .listen import Debouncer, listen_loop
# Imported at module top (not inside main) so a test can monkeypatch
# flax_discover.__main__.write_ack to capture the per-cycle ledger write.
from .persistence import write_ack


log = logging.getLogger("flax-discover")


def _ack_action(summary: dict) -> str:
    """applied if the cycle wrote any device rows this cycle, else noop."""
    return "applied" if summary.get("written") else "noop"


def _ack_cycle(pool, generation, summary: dict) -> None:
    """Success-path consumer_acks write. Wrapped in its own try/except so a
    ledger write failure can NEVER crash the cycle -- the ack is best-effort
    dashboard freshness, not part of the device-write critical path."""
    try:
        write_ack(pool, "flax-discover", "observe_state", generation,
                  _ack_action(summary))
    except Exception:
        log.exception("write_ack (success) failed; continuing")


def _ack_failed(pool, generation, exc) -> None:
    """Except-path consumer_acks write: mark the service unhealthy. detail is
    truncated to 200 chars; these services' exceptions carry no credentials."""
    write_ack(pool, "flax-discover", "observe_state", generation, "failed",
              detail=str(exc)[:200])


def _build_conninfo() -> str:
    """Resolve from env vars (PGHOST, PGUSER, etc.) -- mirrors
    flax_classify._build_conninfo. psycopg DSN keys are host=, port=, user=,
    password=, dbname= (NOT pghost=); map explicitly.
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
    parts.append("application_name=flax-discover")
    return " ".join(parts)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(prog="flax-discover")
    p.add_argument("--cycle-secs", type=float, default=30.0,
                   help="Periodic cycle interval (LISTEN-debounced calls "
                        "happen independently)")
    p.add_argument("--debounce-secs", type=float, default=1.5,
                   help="Coalesce LISTEN pings into one cycle per window")
    p.add_argument("--healthz-port", type=int, default=10995)
    p.add_argument("--healthz-stale-secs", type=float, default=120.0)
    p.add_argument("--family-map-dir", default="/etc/flax/family-map")
    p.add_argument("--match-retry-max-secs", type=float, default=3600.0,
                   help="Back-off ceiling for re-matching unknown families")
    p.add_argument("--vacancy-debounce-secs", type=float, default=600.0,
                   help="A link-down port's devices rows are deleted once "
                        "unseen this long (devices vacancy GC)")
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    conninfo = _build_conninfo()
    pool = build_pool(conninfo)
    health = HealthState(stale_secs=args.healthz_stale_secs)
    disco = Discoverer(family_map_dir=args.family_map_dir,
                       base_secs=args.cycle_secs,
                       max_secs=args.match_retry_max_secs,
                       vacancy_debounce_secs=args.vacancy_debounce_secs)

    # Monotonic per-process cycle counter used as the consumer_acks generation.
    # read_observe_rows does not expose observe_state.generation, and the plan
    # forbids adding a DB query just for it; the dashboard treats generation as
    # informational (it gates on freshness + action), so a counter is the
    # documented fallback. GREATEST in write_ack keeps it monotonic in the row.
    gen_counter = [0]

    def _do_cycle():
        gen_counter[0] += 1
        try:
            summary = disco.run_one_cycle(pool, now=time.monotonic())
            log.info("cycle written=%d superseded=%d swept=%d",
                     summary["written"], summary.get("superseded", 0),
                     summary.get("swept", 0))
            health.record_cycle_done(**summary)
            _ack_cycle(pool, gen_counter[0], summary)
        except Exception as e:
            log.exception("cycle failed")
            _ack_failed(pool, gen_counter[0], e)

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
