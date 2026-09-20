"""Rolling retention sweep: call the work_records_retain policy function.

The policy itself lives in the database (migration 033) so that no role
holds DELETE on the append-only store -- this module only decides WHEN to
call it. Off by default; the post-observe unit is VIP-gated, so the sweep
runs on the primary only. Spec:
docs/superpowers/specs/2026-09-19-work-records-rolling-retention-design.md.
"""
import logging
import os
import time

from .db import get_pool

log = logging.getLogger("flax-post.retention")

def _num(name, default, cast):
    """Numeric env var, falling back to `default` on anything unparseable.

    A typo must not take the post-observe process down at import: this module
    ships disabled, and a bad number alongside ENABLED=false would otherwise
    raise ValueError during `import flax_post.retention`."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw)
    except ValueError:
        log.warning("records-retention: ignoring bad %s=%r, using %r", name, raw, default)
        return default


ENABLED = os.environ.get("FLAX_RECORDS_RETENTION_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on")
KEEP = _num("FLAX_RECORDS_KEEP", 5, int)
INTERVAL_SECS = _num("FLAX_RECORDS_RETENTION_INTERVAL_SECS", 3600.0, float)
MAX_DELETES = _num("FLAX_RECORDS_MAX_DELETES", 20000, int)

_ACTIONS = ("records", "empty_serial_records", "dut_rows", "capped_remaining")
_last_run = None


def run_retention(pool=None, *, keep=None, max_deletes=None, enabled=None,
                  interval_secs=None, now=None):
    """Run one sweep if it is due. Returns the action counts, or None if the
    sweep was skipped (disabled, or inside the interval). Raises on DB errors --
    the caller owns the try/except (lane isolation)."""
    global _last_run
    enabled = ENABLED if enabled is None else enabled
    if not enabled:
        return None
    interval_secs = INTERVAL_SECS if interval_secs is None else interval_secs
    now = time.monotonic() if now is None else now
    if _last_run is not None and now - _last_run < interval_secs:
        return None

    keep = KEEP if keep is None else keep
    max_deletes = MAX_DELETES if max_deletes is None else max_deletes
    pool = pool or get_pool()
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT action, n FROM work_records_retain(%s, %s, %s)",
            (keep, False, max_deletes)).fetchall()
    # Only a sweep that actually ran consumes the interval: a raising DB call
    # leaves _last_run alone so the next pass retries, instead of going quiet
    # for an hour on one transient failure.
    _last_run = now
    counts = {a: int(n) for a, n in rows}
    out = {a: counts.get(a, 0) for a in _ACTIONS}
    if any(out[a] for a in ("records", "empty_serial_records", "dut_rows")):
        log.info("records-retention kept=%d records=%d empty=%d duts=%d capped=%d",
                 keep, out["records"], out["empty_serial_records"],
                 out["dut_rows"], out["capped_remaining"])
    return out
