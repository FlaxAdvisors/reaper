"""In-memory mirror of the switch_facts table, fed by LISTEN/NOTIFY.

Plan 2's flax-switch-sense is the writer of switch_facts. flax-observe
is the first consumer. We mirror the table in memory so per-port workers
don't hammer Postgres on every cycle.
"""
import logging
import threading
from typing import Any

from .db import get_pool


log = logging.getLogger("flax-observe.switch_facts")


def load_all() -> dict[tuple[str, str], dict[str, Any]]:
    """One-shot SELECT to populate the cache. Returns {(switch, port): fact_dict}."""
    pool = get_pool()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT switch, ports FROM switch_facts WHERE reachable = true")
            for switch, ports in cur.fetchall():
                if not isinstance(ports, dict):
                    log.warning("unexpected ports type for %s: %s", switch, type(ports))
                    continue
                for port, fact in ports.items():
                    out[(switch, port)] = fact
    return out


class SwitchFactsCache:
    """Thread-safe live mirror of switch_facts.

    Writers (the LISTEN consumer thread) call _replace_all(). Readers
    (per-port workers) call get_port() — short critical section under an
    RLock.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._by_port: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_refresh: float | None = None

    def _replace_all(self, new_data: dict[tuple[str, str], dict[str, Any]]) -> None:
        import time
        with self._lock:
            self._by_port = new_data
            self._last_refresh = time.time()

    def get_port(self, switch: str, port: str) -> dict[str, Any] | None:
        with self._lock:
            return self._by_port.get((switch, port))

    def ports_for(self, switch: str) -> dict[str, dict[str, Any]]:
        """All known ports for one switch -> {arista_port: fact_dict}.

        Keys are Arista canonical long form (Ethernet10/2), as published by
        flax-switch-sense. Returns a fresh dict (snapshot) so callers can
        iterate outside the lock; an unknown switch yields {}.
        """
        with self._lock:
            return {port: fact for (sw, port), fact in self._by_port.items()
                    if sw == switch}

    def last_refresh_age(self) -> float | None:
        """Seconds since last refresh, or None if never refreshed."""
        import time
        with self._lock:
            if self._last_refresh is None:
                return None
            return time.time() - self._last_refresh

    def refresh_from_db(self) -> None:
        """Pull current snapshot from Postgres into the cache."""
        self._replace_all(load_all())


class SwitchFactsListener(threading.Thread):
    """Long-lived LISTEN switch_facts consumer; refreshes cache on every notify.

    Uses its OWN psycopg connection (NOT the pool) for LISTEN — pool
    connections can't safely block indefinitely on conn.notifies().
    """

    def __init__(self, cache: SwitchFactsCache):
        super().__init__(name="switch-facts-listener", daemon=True)
        self.cache = cache
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        import psycopg
        import os

        dsn = " ".join([
            f"host={os.environ.get('PGHOST', '127.0.0.1')}",
            f"port={os.environ.get('PGPORT', '5432')}",
            f"user={os.environ.get('PGUSER', 'flax_observe')}",
            f"password={os.environ.get('PGPASSWORD', '')}",
            f"dbname={os.environ.get('PGDATABASE', 'flax')}",
            "application_name=flax-observe-listener",
        ])

        while not self._stop_event.is_set():
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    conn.execute("LISTEN switch_facts")
                    self.cache.refresh_from_db()
                    log.info("LISTEN switch_facts active; cache primed with %d ports",
                             len(self.cache._by_port))
                    # generator yields one Notify per pg_notify
                    for notify in conn.notifies(timeout=5.0):
                        if self._stop_event.is_set():
                            break
                        if notify is None:
                            # 5s tick — refresh defensively in case we missed one
                            self.cache.refresh_from_db()
                            continue
                        # Got a notify; refresh the whole cache (cheap)
                        self.cache.refresh_from_db()
            except Exception:
                log.exception("LISTEN connection died, reconnecting in 5s")
                self._stop_event.wait(5.0)
