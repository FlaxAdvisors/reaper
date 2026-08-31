# flax_control/biosfw_view.py
"""Read-only fleet view for the triage BIOS firmware updater.

Reads two already-mounted, read-only sources — no DB, no HTTP to the worker:
  - /etc/flax/host-firmware-versions.json : target manifest
  - /etc/flax/biosfw.json                 : biosfw worker store

"blocked" is the interesting phase here: a node with a known BIOS delta that
the staging gate is deliberately holding (detect mode, or not in the
allowlist). That is the DIMM-debugging hold, and it is what an operator
watches during a campaign. Every other phase (authorized, powering_off,
flashing, powering_on, done, held, fault, up_to_date) means the gate has let
the node through -- the `gate` column on each row collapses that distinction
to just "blocked" vs "authorized" so it reads at a glance, while the `phase`
column keeps the detail.

"held" is the OTHER hold, and it is not the same thing. The flash COMPLETED
and the worker then deliberately did not power the node back on, because an
operator pinned that port off (/etc/flax/bios-fw-hold/<port>) to investigate
it while the rest of the campaign proceeded. It is neither a fault nor a
success, so it gets its own pill and its reason is shown as text in the row
rather than hidden in a tooltip -- the point of the page is seeing at a
glance which nodes are pinned off and why.

THIS PAGE IS READ-ONLY. There is no write path, no POST route and no
writable mount, deliberately: an operator sets and clears a hold by touching
or removing the file on the management host, and the worker picks it up on
its next scan pass. Do not add one.
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
    # "held" sits with the other deliberate holds, NOT with fault and NOT
    # with done: the node is off on purpose and somebody is waiting on a
    # human, but nothing is broken.
    "blocked": "warn", "authorized": "warn", "held": "warn",
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


def note_for(rec):
    """The one line of prose that explains why this row is not just 'done'.

    Ordered by how urgently a human needs to read it:

      1. needs_attention -- the worker declined to power this node on after a
         management-host restart because a BMC-side flash may still be live on
         the shared BIOS SPI. Nothing else on the page outranks that.
      2. held           -- pinned off by an operator; show the reason THEY
         wrote, so the page answers "which nodes are pinned off, and why".
      3. fault          -- distinguishes "still retrying" from "gave up", and
         carries the original fault_reason either way.
    """
    attention = rec.get("needs_attention")
    if attention:
        return str(attention)

    phase = rec.get("phase")
    if phase == "held":
        return (rec.get("hold_reason")
                or "pinned off by an operator; no reason recorded")

    if phase == "fault":
        reason = rec.get("fault_reason") or "no reason recorded"
        attempts = rec.get("attempts")
        limit = rec.get("max_attempts")
        if rec.get("gave_up"):
            if attempts:
                return "gave up after %s attempt%s: %s" % (
                    attempts, "" if attempts == 1 else "s", reason)
            return "gave up: %s" % (reason,)
        if attempts and limit:
            return "retrying (attempt %s of %s): %s" % (attempts, limit, reason)
        return reason

    return rec.get("fault_reason") or ""


def fleet_rows(store):
    """One row per store entry, sorted by port.

    `gate` collapses phase to the two states an operator cares about at a
    glance: "blocked" (the staging gate is holding this node) or
    "authorized" (the gate let it through -- includes every downstream
    phase: powering_off/flashing/powering_on/done/held/fault/up_to_date).
    A `held` node WAS authorized and WAS flashed; what is being withheld is
    its power-on, which is a different question from the gate.
    """
    rows = []
    for port, rec in (store or {}).items():
        phase = rec.get("phase") or "unknown"
        rows.append({
            "port": rec.get("port") or port,
            "current_version": rec.get("current_version") or "—",
            "target_version": rec.get("target_version") or "—",
            "phase": phase,
            "phase_pill": pill_for(phase),
            "gate": "blocked" if phase == "blocked" else "authorized",
            "fault_reason": rec.get("fault_reason") or "",
            "held": phase == "held",
            "hold_reason": rec.get("hold_reason") or "",
            "needs_attention": rec.get("needs_attention") or "",
            "note": note_for(rec),
        })
    rows.sort(key=lambda r: r["port"])
    return rows


def held_count(rows):
    """How many nodes are pinned off right now -- shown in the page header so
    an in-flight hold is visible without reading the table."""
    return sum(1 for r in (rows or []) if r.get("held"))


def attention_count(rows):
    return sum(1 for r in (rows or []) if r.get("needs_attention"))
