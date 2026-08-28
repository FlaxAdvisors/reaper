# flax_control/biosfw_view.py
"""Read-only fleet view for the triage BIOS firmware updater.

Reads two already-mounted, read-only sources — no DB, no HTTP to the worker:
  - /etc/flax/host-firmware-versions.json : target manifest
  - /etc/flax/biosfw.json                 : biosfw worker store

"blocked" is the interesting phase here: a node with a known BIOS delta that
the staging gate is deliberately holding (detect mode, or not in the
allowlist). That is the DIMM-debugging hold, and it is what an operator
watches during a campaign. Every other phase (authorized, powering_off,
flashing, powering_on, done, fault, up_to_date) means the gate has let the
node through -- the `gate` column on each row collapses that distinction to
just "blocked" vs "authorized" so it reads at a glance, while the `phase`
column keeps the detail.
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("flax-control.biosfw_view")

# Overridden in tests via monkeypatch.
FLAX_CONFIG_DIR = os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")

# Worker phase -> CSS pill class (classes already exist in static/style.css).
_STATE_PILL = {
    "up_to_date": "ok", "done": "ok",
    "blocked": "warn", "authorized": "warn",
    "fault": "fail",
    "powering_off": "inprogress", "flashing": "inprogress",
    "powering_on": "inprogress",
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


def read_store():
    """Return the worker store {port: row}, or {} when absent/malformed."""
    data = _read_json("biosfw.json")
    return data if isinstance(data, dict) else {}


def pill_for(phase):
    return _STATE_PILL.get(phase, "neutral")


def targets():
    data = _read_json("host-firmware-versions.json") or {}
    return [{"platform": name,
             "target_version": entry.get("target_version"),
             "auto": entry.get("auto", False)}
            for name, entry in data.items()]


def fleet_rows(store):
    """One row per store entry, sorted by port.

    `gate` collapses phase to the two states an operator cares about at a
    glance: "blocked" (the staging gate is holding this node) or
    "authorized" (the gate let it through -- includes every downstream
    phase: powering_off/flashing/powering_on/done/fault/up_to_date).
    """
    rows = []
    for port, rec in (store or {}).items():
        phase = rec.get("phase") or "unheard-of"
        rows.append({
            "port": rec.get("port") or port,
            "current_version": rec.get("current_version") or "—",
            "target_version": rec.get("target_version") or "—",
            "phase": phase,
            "phase_pill": pill_for(phase),
            "gate": "blocked" if phase == "blocked" else "authorized",
            "fault_reason": rec.get("fault_reason") or "",
        })
    rows.sort(key=lambda r: r["port"])
    return rows
