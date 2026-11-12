# flax_control/bmcfw_view.py
"""Read-only fleet view for the BMC firmware updater.

Joins three already-mounted, read-only sources — no DB, no HTTP to the worker:
  - /etc/flax/bmc-firmware-versions.json : target manifest (TP gate + targets)
  - observe_state_all()                  : product_name per port (TP detection)
  - /etc/flax/bmcfw.json                 : worker store (per-port flash state)

A port is "Tioga Pass" iff its resolved.product_name matches a manifest
platform's product_name regex — the same gate the bmc_fw worker uses.
"""
import datetime
import json
import logging
import os
import re
import time
from pathlib import Path

from .triage_compat import display_port, internal_port

log = logging.getLogger("flax-control.bmcfw_view")

# Overridden in tests via monkeypatch.
FLAX_CONFIG_DIR = os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")

# Worker phase -> CSS pill class (classes already exist in static/style.css).
_STATE_PILL = {
    "up_to_date": "ok", "done": "ok",
    "needs_update": "warn",
    "fault": "fail",
    "flashing": "inprogress", "monitoring": "inprogress", "activating": "inprogress",
    "not evaluated": "neutral",
}


def _read_json(name):
    try:
        with open(Path(FLAX_CONFIG_DIR) / name) as f:
            return json.load(f)
    except OSError:
        return None
    except json.JSONDecodeError as exc:
        log.warning("malformed JSON in %s: %s", name, exc)
        return None


class _Matcher:
    """Compiled product_name gate built from the manifest."""

    def __init__(self, data):
        self._data = data or {}
        self._compiled = {
            name: [re.compile(p) for p in entry.get("match", {}).get("product_name", [])]
            for name, entry in self._data.items()
        }

    def match(self, product_name):
        if not product_name:
            return None
        for name, entry in self._data.items():
            if any(rx.search(product_name) for rx in self._compiled[name]):
                return name, entry.get("target_version")
        return None

    @property
    def targets(self):
        return [{"platform": name,
                 "target_version": entry.get("target_version"),
                 "auto": entry.get("auto", False)}
                for name, entry in self._data.items()]


def load_matcher():
    """Build a _Matcher from the manifest. Missing/malformed -> matches nothing."""
    return _Matcher(_read_json("bmc-firmware-versions.json") or {})


def read_store():
    """Return the worker store {port: row}, or {} when absent/malformed."""
    data = _read_json("bmcfw.json")
    return data if isinstance(data, dict) else {}


def _format_duration(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def _age(ts):
    if not ts:
        return ""
    return _format_duration(time.time() - ts)


def store_last_updated(store):
    """Max updated_at (epoch float) across worker-store rows, or None."""
    ts = [r.get("updated_at") for r in (store or {}).values()
          if isinstance(r, dict) and r.get("updated_at")]
    return max(ts) if ts else None


def fmt_ts(epoch):
    """UTC stamp for a page-freshness line, e.g. '2026-07-07T22:54Z'."""
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def fmt_updated(epoch, now):
    """Readable freshness string for a store timestamp against a reference
    `now`, e.g. '2026-07-07T22:54Z (3d ago)' — or 'never' when epoch is
    None/0 (store has no evaluated rows yet). Age math lives here, in
    Python, not in the template."""
    if not epoch:
        return "never"
    return "%s (%s ago)" % (fmt_ts(epoch), _format_duration(now - epoch))


def _port_sort_key(disp):
    m = re.match(r"^Et(\d+)/(\d+)$", disp)
    if m:
        return (0, int(m.group(1)), int(m.group(2)), disp)
    return (1, 0, 0, disp)


def fleet_rows(observe_state, store, matcher):
    """One row per Tioga Pass port, joining manifest + observe_state + store."""
    rows = []
    for obs in observe_state.values():
        resolved = obs.get("resolved") or {}
        hit = matcher.match(resolved.get("product_name"))
        if hit is None:
            continue
        _platform, manifest_target = hit
        port = internal_port(obs["port"])
        s = store.get(port) or {}
        evaluated = bool(s)
        state = s.get("phase") if evaluated else "not evaluated"
        rows.append({
            "port": display_port(port),
            "bmc_ip": s.get("bmc_ip") or resolved.get("bmc_ip") or "",
            "current_version": s.get("current_version") or "—",
            "target_version": s.get("target_version") or manifest_target or "—",
            "state": state,
            "state_pill": _STATE_PILL.get(state, "neutral"),
            "percent": s.get("percent") if evaluated else None,
            "fault_reason": s.get("fault_reason") or "",
            "updated": _age(s.get("updated_at")) if evaluated else "",
            "evaluated": evaluated,
        })
    rows.sort(key=lambda r: _port_sort_key(r["port"]))
    return rows
