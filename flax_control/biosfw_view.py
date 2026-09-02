# flax_control/biosfw_view.py
"""Read-only fleet view for the triage BIOS firmware updater.

Reads two already-mounted, read-only sources — no DB, no HTTP to the worker:
  - /etc/flax/host-firmware-versions.json : target manifest
  - /etc/flax/biosfw.json                 : biosfw worker store

"blocked" USED TO BE the interesting phase here, and is not any more. It once
meant "a node with a known BIOS delta that the staging gate is deliberately
holding". Since the worker began keeping a row for every managed candidate
(2026-09-02), it is the DEFAULT phase of a perfectly healthy node that has
simply not reported in band yet, and blocked rows outnumber everything else.

Keying "interesting" off the phase alone therefore painted the whole page
amber for a healthy fleet while a genuinely confirmed-good node rendered
grey -- the signal exactly inverted. So interest is decided from the ROW, not
the phase: a blocked row is only held-with-a-delta when a current AND a target
version are both known and differ. `pill_for` stays phase-only for callers
that have nothing else; `pill_for_row` is what the table uses.

The `gate` column is three-valued for the same reason:
  "blocked"     -- a real delta the staging gate is holding (the DIMM-
                   debugging hold): the thing an operator watches in a campaign
  "no report"   -- blocked, but nothing is known about this node's BIOS yet.
                   Not a problem, and must not look like one.
  "cleared"     -- the node reported in band at target and the gate confirmed
                   it. This is the only state that proves the node BOOTED AND
                   RAN that BIOS.
  "authorized"  -- the gate let it through: every downstream phase
                   (powering_off/flashing/powering_on/done/held/fault/
                   up_to_date). A `held` node WAS authorized and WAS flashed;
                   what is withheld is its power-on, a different question.

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
    # "confirmed" is the only green that means the node BOOTED AND RAN this
    # BIOS -- an in-band report through the gate. It outranks every other ok.
    "up_to_date": "ok", "done": "ok", "confirmed": "ok",
    # Terminal and waiting on hands at the rack: the flash succeeded but the
    # PSU never returned power-good, so only pulling the sled clears it. Red,
    # because no amount of waiting or retrying fixes it.
    "needs_power_cycle": "fail",
    # "held" sits with the other deliberate holds, NOT with fault and NOT
    # with done: the node is off on purpose and somebody is waiting on a
    # human, but nothing is broken.
    # NOT warn any more: blocked is the healthy default for a node that has
    # not reported in band. pill_for_row() re-raises it to warn when the row
    # actually carries a known delta.
    "blocked": "neutral",
    "authorized": "warn", "held": "warn",
    "fault": "fail",
    "powering_off": "inprogress", "flashing": "inprogress",
    "verifying": "inprogress", "powering_on": "inprogress",
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
    """Phase -> pill, for callers holding nothing but a phase string."""
    return _STATE_PILL.get(phase, "neutral")


def has_known_delta(rec):
    """True when this row actually knows the node is off target.

    Both versions present AND different. An empty current_version means "no
    in-band report yet", which is not a delta -- treating it as one is what
    turned a healthy fleet amber."""
    cur = (rec or {}).get("current_version") or ""
    tgt = (rec or {}).get("target_version") or ""
    return bool(cur) and bool(tgt) and cur != tgt


def pill_for_row(rec):
    """Row -> pill. Use this for the table; `pill_for` cannot see the delta."""
    phase = (rec or {}).get("phase") or "unknown"
    if phase == "blocked" and has_known_delta(rec):
        return "warn"
    return pill_for(phase)


def gate_for(rec):
    """The three-valued gate column -- see the module docstring."""
    phase = (rec or {}).get("phase") or "unknown"
    if phase == "confirmed":
        return "cleared"
    if phase == "blocked":
        return "blocked" if has_known_delta(rec) else "no report"
    return "authorized"


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
      2. held / hold_set -- pinned off by an operator; show the reason THEY
         wrote, so the page answers "which nodes are pinned off, and why".
         hold_set covers a hold on a row that has NOT reached `held` (a
         faulted, blocked or not-yet-flashed node), which is otherwise
         invisible -- the operator gets no confirmation the hold took.
      3. fault          -- distinguishes "still retrying" from "gave up", and
         carries the original fault_reason either way.

    More than one can apply (a held node that also needs attention, a faulted
    node that has just been pinned), so they are joined rather than ranked to
    a single winner.
    """
    parts = []

    attention = rec.get("needs_attention")
    if attention:
        parts.append(str(attention))

    phase = rec.get("phase")
    hold_reason = rec.get("hold_reason") or ""
    # A stranded_by_hold row's needs_attention text already quotes the
    # operator's note, so repeating it below reads like two separate holds.
    # This covers BOTH shapes: the hold_set form, and the `held` form -- a
    # held row that startup recovery also flagged, which its gate branch does
    # produce (a held row with a leftover authorized gate entry).
    hold_already_stated = bool(attention) and bool(rec.get("stranded_by_hold"))
    if phase == "held":
        if not hold_already_stated:
            parts.append(hold_reason
                         or "pinned off by an operator; no reason recorded")
    elif rec.get("hold_set") and not hold_already_stated:
        # A hold file exists for this port but the row has not reached `held`
        # -- the operator pinned a node that is faulted, blocked, mid-sequence
        # or not yet known. Saying so is the whole point: otherwise the one
        # confirmation that the hold took never appears anywhere.
        parts.append("hold set, power-on will be withheld"
                     + (": " + hold_reason if hold_reason else ""))

    if phase == "fault":
        reason = rec.get("fault_reason") or "no reason recorded"
        attempts = rec.get("attempts")
        limit = rec.get("max_attempts")
        if rec.get("gave_up"):
            if attempts:
                parts.append("gave up after %s attempt%s: %s" % (
                    attempts, "" if attempts == 1 else "s", reason))
            else:
                parts.append("gave up: %s" % (reason,))
        elif attempts and limit:
            parts.append("retrying (attempt %s of %s): %s"
                         % (attempts, limit, reason))
        else:
            parts.append(reason)

    if not parts:
        return rec.get("fault_reason") or ""
    return " · ".join(parts)


def fleet_rows(store):
    """One row per store entry, sorted by port.

    `gate` is three-valued -- blocked / no report / cleared / authorized -- and
    `phase_pill` comes from pill_for_row, not pill_for, so a blocked row is
    only amber when it carries a known delta. Both are explained at length in
    the module docstring; the short version is that "blocked" stopped meaning
    "interesting" the day the worker started keeping a row per candidate.
    """
    rows = []
    for port, rec in (store or {}).items():
        phase = rec.get("phase") or "unknown"
        rows.append({
            "port": rec.get("port") or port,
            "current_version": rec.get("current_version") or "—",
            "target_version": rec.get("target_version") or "—",
            "phase": phase,
            "phase_pill": pill_for_row(rec),
            "gate": gate_for(rec),
            "fault_reason": rec.get("fault_reason") or "",
            "held": phase == "held",
            # hold_set answers "does this PORT have a hold file", which is a
            # different question from "has this ROW reached phase held" -- a
            # hold on a faulted or not-yet-flashed node shows up only here.
            "hold_set": bool(rec.get("hold_set")) or phase == "held",
            "hold_reason": rec.get("hold_reason") or "",
            "needs_attention": rec.get("needs_attention") or "",
            "note": note_for(rec),
        })
    rows.sort(key=lambda r: r["port"])
    return rows


def held_count(rows):
    """How many PORTS have a hold set right now -- shown in the page header so
    a hold is visible without reading the table.

    Counts hold_set, not phase == "held": an operator who pins a node that is
    currently faulted, blocked, or not yet flashed must still get confirmation
    on the page that the hold took."""
    return sum(1 for r in (rows or []) if r.get("hold_set"))


def attention_count(rows):
    return sum(1 for r in (rows or []) if r.get("needs_attention"))
