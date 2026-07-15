"""Per-switch poll-loop thread + config/driver wiring."""
import json
import logging
import threading
from typing import Optional

from .db import get_pool
from .driver_cumulus import CumulusDriver
from .driver_eos import EosDriver
from .driver_ios import IosDriver
from .publisher import build_switch_facts_row, write_switch_facts, write_ack
from .slice import slice_by_port


log = logging.getLogger("flax-switch-sense")


class ConfigError(Exception):
    pass


def load_switches(path: str) -> list[dict]:
    """Load /etc/flax/switches.json. Expected schema (list of dicts):
    [{"name": "<switch>", "driver": "eos|cumulus|ios", "host": "<dns-or-ip>"}]"""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ConfigError(f"{path}: expected list, got {type(data).__name__}")
    for entry in data:
        for key in ("name", "driver", "host"):
            if key not in entry:
                raise ConfigError(f"{path}: entry {entry!r} missing key {key!r}")
    return data


def make_driver(entry: dict, credentials: dict):
    """Construct a driver instance for the given switches.json entry.

    The deployed /etc/flax/credentials.json (shared with reaper-leased +
    switchportrecond) uses flat keys: eosuser/eospass for Arista, cumuser/
    cumpass for Cumulus, etc. We mirror that shape rather than introducing
    a new nested layout."""
    driver_kind = entry["driver"]
    if driver_kind == "eos":
        return EosDriver(
            host=entry["host"],
            user=credentials.get("eosuser", ""),
            password=credentials.get("eospass", ""),
        )
    if driver_kind == "cumulus":
        return CumulusDriver(
            host=entry["host"],
            user=credentials.get("cumuser", ""),
            password=credentials.get("cumpass", ""),
        )
    if driver_kind == "ios":
        # Cisco IOS classic (e.g. eindhoven's turtle-gouda 3750X). Flat creds
        # cisco_user/cisco_pass, matching flax_reconcile.drivers (the IOS writer)
        # + reaper_leased's credentials.json convention.
        return IosDriver(
            host=entry["host"],
            user=credentials.get("cisco_user", ""),
            password=credentials.get("cisco_pass", ""),
        )
    raise ConfigError(
        f"unsupported driver {driver_kind!r} for switch {entry['name']!r}"
    )


def driver_name(driver) -> str:
    return type(driver).__name__.replace("Driver", "").lower()


class SwitchFetcher(threading.Thread):
    """One thread per switch. Polls every cycle_secs, writes switch_facts."""

    def __init__(self, switch_name: str, driver, *,
                 cycle_secs: float = 10.0,
                 macmath_by_vid: dict | None = None):
        super().__init__(name=f"fetcher-{switch_name}", daemon=True)
        self.switch_name = switch_name
        # Per-switch consumer_acks source so each switch's health is an
        # independent ledger row. A single shared "switches" row was last-write-
        # wins: a healthy switch's frequent "applied" ack clobbered a failing
        # switch's "failed" ack within a cycle, hiding partial outages from the
        # control dashboard (which reads consumer_acks). See consumer_health().
        self._ack_source = f"switches/{switch_name}"
        self.driver = driver
        self.cycle_secs = cycle_secs
        # {vid: config} from macmath.load_macmath_dir; default {} -> every
        # port classifies with the legacy pairing (tests stay unchanged).
        self.macmath_by_vid = macmath_by_vid or {}
        self._stop = threading.Event()
        self.last_generation: Optional[int] = None
        self.last_error: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                log.exception("poll failed for %s", self.switch_name)
                self.last_error = str(e)
                # Mark the consumer_acks ledger unhealthy so the dashboard
                # Pipeline tile surfaces the failing fetcher. last_generation
                # may be None on the very first failed cycle -> coerce to 0.
                try:
                    write_ack(get_pool(), "flax-switch-sense", self._ack_source,
                              getattr(self, "last_generation", 0) or 0,
                              "failed", detail=str(e)[:200])
                except Exception:
                    log.exception("could not write failed ack for %s",
                                  self.switch_name)
                # Write an unreachable row so consumers see the stale flag
                try:
                    row = build_switch_facts_row(
                        self.switch_name,
                        driver_name(self.driver),
                        {},
                        reachable=False,
                        macmath_by_vid=self.macmath_by_vid,
                    )
                    self.last_generation = write_switch_facts(row)
                except Exception:
                    log.exception("could not write unreachable switch_facts for %s",
                                  self.switch_name)
            # Sleep until next cycle (interruptible)
            self._stop.wait(self.cycle_secs)

    def _poll_once(self) -> None:
        # Take a single per-poll snapshot first. For the IOS driver this is one
        # batched ssh session (one connection, no ControlMaster zombie); eAPI/
        # NCLU drivers no-op and fetch live per read.
        self.driver.refresh()
        link = self.driver.interfaces_status()
        vlans = self.driver.vlans()
        macs = self.driver.mac_address_table()
        lldp = self.driver.lldp_neighbors_detail()
        per_port = slice_by_port(link, vlans, macs, lldp)
        row = build_switch_facts_row(
            self.switch_name, driver_name(self.driver),
            per_port, reachable=True,
            macmath_by_vid=self.macmath_by_vid,
        )
        self.last_generation = write_switch_facts(row)
        self.last_error = None
        # Ack the consumer_acks high-water-mark for this fetcher. Few fetchers
        # -> low contention; GREATEST in write_ack keeps the max generation
        # across switches on the single (flax-switch-sense, switches) row.
        # Wrapped so a ledger write failure can NEVER break polling.
        try:
            write_ack(get_pool(), "flax-switch-sense", self._ack_source,
                      self.last_generation, "applied")
        except Exception:
            log.exception("write_ack (applied) failed for %s; continuing",
                          self.switch_name)
