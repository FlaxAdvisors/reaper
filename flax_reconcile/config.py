"""Tunables loaded from /etc/flax/reconcile.json (spec §14).

Absent keys fall back to DEFAULTS (reaper-leased's current values where one
exists). Unknown keys are ignored (forward-compat). A malformed file is fatal —
we fail loud rather than run on stale assumptions.
"""
import json
import logging

log = logging.getLogger("flax-reconcile.config")

DEFAULTS = {
    "sweep_interval_secs": 900,        # reaper run_periodic 15-min worker
    "flap_hold_seconds": 2,            # driver FLAP_HOLD_SECONDS
    "kick_cooldown_secs": 60,          # NAK_FLAP_COOLDOWN_SECONDS
    "bmc_ll_probe_interval_secs": 60,  # IPV6_LL_PROBE_INTERVAL_SECS
    "max_attempts": 3,                 # new: queue 'stuck' threshold
    "sentinel_grace_secs": 30,         # new: link-up settling slack
    "debounce_secs": 2,                # flax-* LISTEN debounce convention
    "flap_circuit_threshold": 3,       # flaps in window before backoff
    "flap_circuit_window_secs": 300,   # rolling window length (seconds)
    "flap_circuit_backoff_secs": 900,  # how long to hold off after threshold hit
    "reclaim_stale_claim_secs": 180,   # crash-stranded 'claimed' row reclaim age
}


def load_config(path: str) -> dict:
    """Return effective config: DEFAULTS overlaid with known keys from `path`."""
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        log.info("no %s; using built-in defaults", path)
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"malformed reconcile config {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"reconcile config {path} must be a JSON object")
    for k, v in raw.items():
        if k in DEFAULTS:
            cfg[k] = v
        else:
            log.warning("ignoring unknown config key %r", k)
    log.info("effective reconcile config: %s", cfg)
    return cfg
