"""Switch config + driver construction, free of the daemon's postgres side.

Split out of fetcher.py so a stdlib consumer (rackpost, flax_rack.switches) can
build the same drivers without importing psycopg; fetcher re-exports every name."""
import json

from .driver_cumulus import CumulusDriver
from .driver_eos import EosDriver
from .driver_ios import IosDriver
from .driver_sonic import SonicDriver


class ConfigError(Exception):
    pass


def load_switches(path: str) -> list[dict]:
    """Load /etc/flax/switches.json. Expected schema (list of dicts):
    [{"name": "<switch>", "driver": "eos|cumulus|ios|sonic", "host": "<dns-or-ip>"}]"""
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
    if driver_kind == "sonic":
        # Community SONiC (e.g. the Dell N3248TE / Wedge100S rack switches).
        # Flat creds sonicuser/sonicpass, as the rackpost readers use.
        return SonicDriver(
            host=entry["host"],
            user=credentials.get("sonicuser", ""),
            password=credentials.get("sonicpass", ""),
        )
    raise ConfigError(
        f"unsupported driver {driver_kind!r} for switch {entry['name']!r}"
    )


def driver_name(driver) -> str:
    return type(driver).__name__.replace("Driver", "").lower()
