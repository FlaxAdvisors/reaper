"""Build complete blade records: consumed (switch_facts/kea) ⋈ state ⋈ geometry.

Output is the full slot grid in rack order so the UI renders empties; populated
slots are complete, well-shaped records. Each phase's steps are driven by its
producer's post_state.vars slice: Discover from consumed switch/kea facts,
Firmware from vars.fw_bmc/fw_bios/fw_nic + power_on, Qualify from vars.qual
(the host_qual poller) + vars.pop, Done from vars.done. Phase advances
Discover -> Firmware -> Qualify -> Done as each phase's steps all reach 'done'.
"""
import os

from . import geometry
from .consume import _link_value
from .nicd.classify import aggregate as _nic_aggregate

SWITCH = "rabbit-edam"
PHASES = ("Discover", "Firmware", "Qualify", "Done")


def post_switch(geo) -> str:
    """The post rack switch for the viewer: the first rack declared in the loaded
    geometry (braintree rabbit-lorax vs eindhoven rabbit-edam), falling back to
    the module default when no rack is declared. Keeps the viewer site-agnostic
    instead of hardcoding a single site's switch."""
    return next(iter(geo.get("racks") or {}), SWITCH)

# Slot-ladder rungs (spec 2026-09-11 post-slot-ladder §4). observe/ladder.py
# imports these so the tile and the machine never disagree on names/order.
RUNGS = ("bmc-pinged", "power-on", "tftp-seen", "ipxe-seen", "host-leased",
         "live-iso-seen", "host-pinged", "host-ssh", "fw-gates", "agent-reachable",
         "qualify", "done")
BOOT_MARKER_RUNGS = ("tftp-seen", "ipxe-seen", "live-iso-seen")
# Rung budgets (seconds); None = never times out. Lives here (not in
# observe/ladder.py) so the tile can show "budget N s" without importing the
# machine, which imports this module. Env: FLAX_POST_LADDER_<RUNG>_S.
LADDER_BUDGET_S = {
    "power-on": 60, "tftp-seen": 600, "ipxe-seen": 120, "host-leased": 120,
    "live-iso-seen": 300, "host-pinged": 600, "host-ssh": 120, "fw-gates": 600,
    "agent-reachable": 180,
    # Not a rung: how long the power-on rung tolerates an UNREADABLE power
    # (BMC off the network after the chassis power-on) before faulting.
    "power-unreadable": 600,
}


def ladder_budget_s(rung):
    """Seconds allowed on `rung`, None for rungs that never time out."""
    env = os.environ.get("FLAX_POST_LADDER_%s_S" % rung.replace("-", "_").upper())
    if env:
        return int(env)
    return LADDER_BUDGET_S.get(rung)
_RUNG_INDEX = {r: i for i, r in enumerate(RUNGS)}

DISCOVER_STEPS = (
    "switchportlink", "bmc-mac-seen", "bmc-reserved", "bmc-leased", "bmc-pinged",
    "serial", "power-on", "tftp-seen", "ipxe-seen", "host-mac-seen", "host-reserved",
    "host-leased", "live-iso-seen", "host-pinged", "host-ssh",
)
# Discover steps whose truth comes from the ladder slice (the rest come from
# consumed facts + the IPMI lanes).
_LADDER_STEPS = ("power-on", "tftp-seen", "ipxe-seen", "live-iso-seen", "host-pinged", "host-ssh")
PHASE_STEPS = {
    "Discover": DISCOVER_STEPS,
    "Firmware": ("bmc-checked", "bmc-updated", "bios-checked", "bios-updated",
                 "mlx-checked", "mlx-updated"),
    "Qualify": ("agent-reachable", "sdr-pre", "sel-pre", "sel-clear", "tooling",
                "inventory", "fio", "population-check", "iperf", "mem-pre",
                "cpu-mem-stress", "mem-post", "sdr-post", "sel-post", "console"),
    "Done": ("identify", "power-off", "done"),
}
_COL_ORDER = {"L": 0, "C": 1, "R": 2, "A": 0, "B": 1, "D": 3, "full": 0}


def _ladder_step_status(lad: dict, step: str) -> str:
    """done|cur|pending|fault|skip for one ladder-derived Discover step."""
    cur = lad.get("rung")
    ci = _RUNG_INDEX.get(cur, -1)
    ri = _RUNG_INDEX[step]
    fault = lad.get("fault") or {}
    if fault.get("rung") == step:
        return "fault"
    if step in BOOT_MARKER_RUNGS and (lad.get("marks") or {}).get("skipped") and ri < ci:
        # Evidence never collected (power already on when the worker looked):
        # `unknown`, not `skip`. It does not hold the phase, but it is not a
        # completed step either, so the bar never reads green on it.
        return "unknown"
    if ri < ci:
        return "done"
    if ri == ci:
        return "cur"
    return "pending"


def _ladder_fallback_status(st: dict, step: str) -> str:
    """No ladder slice yet (row predates the worker, or observe is mid-upgrade):
    derive what live signals can, mark the boot markers unknown so Discover
    does not hold the phase (but is not green either)."""
    if step == "power-on":
        return "done" if st.get("power_on") == "on" else "cur"
    if step in BOOT_MARKER_RUNGS:
        return "unknown"
    return "done" if st.get("host_pinged") else "cur"      # host-pinged, host-ssh


def _discover_steps(c: dict, st: dict, live_link) -> dict:
    """Every Discover step -> done|cur|pending|fault|skip. Consumed/IPMI steps
    follow the first-undone-is-cur rule; ladder steps come from the slice."""
    lad = st.get("ladder") or {}
    truth = {
        "switchportlink": live_link == "link",
        "bmc-mac-seen": bool(c.get("bmc_mac_seen")),
        "bmc-reserved": bool(c.get("bmc_reserved")),
        "bmc-leased": bool(c.get("bmc_leased")),
        "bmc-pinged": bool(st.get("bmc_pinged")),
        "serial": bool(st.get("serial")),
        "host-mac-seen": bool(c.get("host_mac_seen")),
        "host-reserved": bool(c.get("host_reserved")),
        "host-leased": bool(c.get("host_leased")),
    }
    fault_rung = (lad.get("fault") or {}).get("rung")
    out, seen_cur = {}, False
    for name in DISCOVER_STEPS:
        if name in _LADDER_STEPS:
            s = _ladder_step_status(lad, name) if lad else _ladder_fallback_status(st, name)
            if s == "cur" and seen_cur:
                s = "pending"
            if s in ("cur", "fault"):
                seen_cur = True
            out[name] = s
            continue
        if name == fault_rung:
            # A ladder rung that is ALSO a consumed-fact step (host-leased):
            # the fact is simply absent, so the first-undone-is-cur rule would
            # render it `cur` and the fault would never reach the tile.
            out[name] = "fault"
            seen_cur = True
            continue
        if seen_cur:
            out[name] = "pending"
        elif truth[name]:
            out[name] = "done"
        else:
            out[name] = "cur"; seen_cur = True
    return out


# fwd phase -> (bmc-checked state, bmc-updated state)
_FW_BMC = {
    "up_to_date": ("done", "done"), "done": ("done", "done"),
    "needs_update": ("done", "cur"),
    "checking": ("cur", "pending"),
    "flashing": ("done", "cur"), "monitoring": ("done", "cur"),
    "activating": ("done", "cur"),
    "fault": ("done", "fault"),
    "unreachable": ("cur", "pending"),   # attention; re-classifies when the BMC returns
    "oem": ("done", "done"),             # reachable Redfish OEM board, nothing to flash (terminal)
    "unsupported": ("done", "done"),     # nothing to flash here; _GATE_PASS agrees
}

# biosd phase -> (bios-checked state, bios-updated state)
_FW_BIOS = {
    "up_to_date": ("done", "done"), "done": ("done", "done"),
    "needs_update": ("done", "cur"),
    "checking": ("cur", "pending"),
    "flashing": ("done", "cur"), "activating": ("done", "cur"),
    "fault": ("done", "fault"),
    "unreachable": ("cur", "pending"), "unknown": ("cur", "pending"),
    "unsupported": ("done", "done"),   # nothing to do on this platform
}


def _nic_steps(st):
    """(mlx-checked, mlx-updated) from post_state.vars.fw_nic. Unlike bmc/bios
    (scalar phase maps), NIC aggregates a per-card device list; no slice yet ->
    pending/pending."""
    nic = st.get("fw_nic")
    if not nic:
        return "pending", "pending"
    if nic.get("phase") == "unreachable":
        return "cur", "pending"
    if nic.get("phase") == "fault":
        return "done", "fault"
    checked, updated, _roll = _nic_aggregate(nic.get("devices") or [])
    return checked, updated


# The ONE Firmware gate (spec 2026-09-11 post-slot-ladder §7), used by the
# slot ladder's fw-gates rung and by the tile's phase derivation. In detect
# mode nothing will ever flash a needs_update blade, so the gate lets it
# through and the tile keeps showing the update step as `cur`.
_GATE_PASS = frozenset({"up_to_date", "done", "oem", "unsupported"})


def fw_gate_passed(slice_, mode=None) -> bool:
    """True when this firmware slice no longer blocks Qualify. A missing mode
    on the row reads as 'detect' (rows written before the field existed)."""
    if not slice_:
        return False
    phase = slice_.get("phase")
    if phase in _GATE_PASS:
        return True
    if phase == "needs_update":
        return (mode or slice_.get("mode") or "detect") == "detect"
    return False


# Which Firmware step a fw-gates ladder fault lands on, in gate order.
_GATE_STEPS = (("fw_bmc", "bmc-updated"), ("fw_bios", "bios-updated"),
               ("fw_nic", "mlx-updated"))


def fw_gate_fault_step(st):
    """The Firmware step a `fw-gates` ladder fault renders on: the first slice
    that does not pass the gate (BMC, then BIOS, then NIC). `fw-gates` is not
    itself a tile step, so without this the fault and its note are invisible.
    None when the ladder is not faulted at fw-gates (or, degenerately, when
    every gate passes after the fault was recorded)."""
    if ((st.get("ladder") or {}).get("fault") or {}).get("rung") != "fw-gates":
        return None
    for key, step in _GATE_STEPS:
        if not fw_gate_passed(st.get(key)):
            return step
    return None


def _firmware_steps(st):
    """Explicit done|cur|pending|fault per Firmware step from fw_bmc + fw_bios +
    fw_nic. BMC before BIOS before NIC; key order matches PHASE_STEPS['Firmware'].
    """
    bmc = st.get("fw_bmc")
    chk, upd = _FW_BMC.get((bmc or {}).get("phase"), ("pending", "pending"))
    bios = st.get("fw_bios")
    bchk, bupd = _FW_BIOS.get((bios or {}).get("phase"), ("pending", "pending"))
    mchk, mupd = _nic_steps(st)
    out = {"bmc-checked": chk, "bmc-updated": upd,
           "bios-checked": bchk, "bios-updated": bupd,
           "mlx-checked": mchk, "mlx-updated": mupd}
    gate_fault = fw_gate_fault_step(st)
    if gate_fault:
        out[gate_fault] = "fault"
    return out


def fw_gates_passed(st) -> bool:
    """All three firmware slices pass fw_gate_passed (spec §7)."""
    return all(fw_gate_passed(st.get(k)) for k in ("fw_bmc", "fw_bios", "fw_nic"))


_QUAL_MAP = {"pass": "done", "running": "cur", "pending": "pending",
             "fail": "fault", "skip": "skip"}
# Step states that COMPLETE a phase for the bar (green): done, or a test that
# ran and found itself N/A (fio on a diskless blade).
_COMPLETE = ("done", "skip")
# Step states that do not HOLD the phase: the above plus `unknown` (evidence
# the ladder never collected — the phase still advances, the bar stays grey).
_ADVANCES = _COMPLETE + ("unknown",)

# Skip reasons the agent emits -> the short label the tile shows next to the step.
_SKIP_LABELS = {"no physical storage media": "no storage"}


def phase_done(steps: dict) -> bool:
    """A phase is complete (green) when every step is done or skipped."""
    return all(v in _COMPLETE for v in steps.values())


def phase_advances(steps: dict) -> bool:
    """Nothing in this phase holds the pipeline: done, skipped or unknown."""
    return all(v in _ADVANCES for v in steps.values())


def _qualify_steps(st):
    """Per-step done|cur|pending|fault|skip for Qualify, from post_state.vars.qual.steps.
    Missing step -> pending (producer hasn't reached it)."""
    qsteps = (st.get("qual") or {}).get("steps") or {}
    out = {}
    for name in PHASE_STEPS["Qualify"]:
        status = (qsteps.get(name) or {}).get("status", "pending")
        out[name] = _QUAL_MAP.get(status, "pending")
    # population-check is the engine's own step and vars.pop is its own slice,
    # written by the same poll; read it from there so the verdict survives a
    # later rewrite of qual.steps (rows from before the latch lost them).
    popv = (st.get("pop") or {}).get("verdict")
    if popv in _POP_MAP:
        out["population-check"] = _POP_MAP[popv]
    # agent-reachable is a ladder rung AND the first Qualify step; the agent
    # never writes a qual step for it, so its fault has to come from the slice.
    if ((st.get("ladder") or {}).get("fault") or {}).get("rung") == "agent-reachable":
        out["agent-reachable"] = "fault"
    return out


_POP_MAP = {"green": "done", "red": "fault", "grey": "pending"}


def _step_notes(st) -> dict:
    """{step: short text} for skipped Qualify steps, from the agent's summary.reason.
    Rendered INLINE next to the step; timing faults are deliberately not here
    (they go to the step modal via _fault_notes) so the pipeline table stays terse."""
    qsteps = (st.get("qual") or {}).get("steps") or {}
    notes = {}
    for name, rec in qsteps.items():
        rec = rec or {}
        if rec.get("status") != "skip":
            continue
        reason = (rec.get("summary") or {}).get("reason") or "skipped"
        notes[name] = _SKIP_LABELS.get(reason, reason)
    return notes


def _fault_notes(st) -> dict:
    """{step: reason} for the ladder's fault and its skipped-markers note. The
    tile's step modal shows these; the inline row shows only the red icon."""
    notes = {}
    lad = st.get("ladder") or {}
    fault = lad.get("fault") or {}
    if fault.get("rung") and fault.get("reason"):
        # fw-gates is not a tile step: its note rides on the same Firmware step
        # the fault renders on.
        notes[fw_gate_fault_step(st) or fault["rung"]] = fault["reason"]
    if (lad.get("marks") or {}).get("skipped"):
        notes["tftp-seen"] = "skipped: " + lad["marks"]["skipped"]
    return notes


def _ladder_view(st) -> dict:
    """The ladder slice as the step modal renders it: rung, its clock and budget,
    each boot mark, the skipped note and the fault. {} when no ladder yet."""
    lad = st.get("ladder") or {}
    if not lad:
        return {}
    rung = lad.get("rung")
    marks = lad.get("marks") or {}
    return {"rung": rung, "since": lad.get("since"),
            "budget_s": ladder_budget_s(rung) if rung else None,
            "power_on_at": lad.get("power_on_at"),
            "marks": {k: v for k, v in marks.items() if k != "skipped"},
            "skipped": marks.get("skipped"), "fault": lad.get("fault")}


# Phases whose step maps are frozen into done.steps when a verdict lands
# (ruling 2026-09-12). Their live sources regress the moment the Done tail
# powers the blade off: host-leased/host-mac-seen follow the lease and the
# switch FDB, and biosd/nicd cannot ssh an off host. Qualify (vars.qual) and
# Done (vars.done) already only move on a new run.
LATCHED_PHASES = ("Discover", "Firmware")


def latch_snapshot(steps) -> "dict | None":
    """The Discover + Firmware step maps of a blade record, or None when the
    record has neither (a poll_target caller without a record, or a snapshot
    that would carry nothing)."""
    if not isinstance(steps, dict):
        return None
    snap = {p: dict(steps[p]) for p in LATCHED_PHASES if isinstance(steps.get(p), dict)}
    return snap or None


def _latched_steps(st, steps: dict) -> dict:
    """`steps` with Discover/Firmware replaced by the verdict-time snapshot
    (done.steps) when the row is latched and carries one. Keys come out in
    PHASE_STEPS order; a step the snapshot lacks reads pending. A latched row
    with no snapshot (written before the ruling) keeps its live maps: those
    holes are real and must show."""
    done = st.get("done") or {}
    if done.get("verdict") is None:
        return steps
    snap = done.get("steps")
    if not isinstance(snap, dict):
        return steps
    out = dict(steps)
    for p in LATCHED_PHASES:
        frozen = snap.get(p)
        if isinstance(frozen, dict):
            out[p] = {name: frozen.get(name, "pending") for name in PHASE_STEPS[p]}
    return out


def _done_steps(st):
    """identify -> power-off -> done, from post_state.vars.done. No verdict -> all
    pending; a fail verdict leaves them pending (node stays powered). A tail step
    the engine could not verify (power_off/identify == 'fault') renders as fault."""
    done = st.get("done") or {}
    if done.get("verdict") != "pass":
        return {s: "pending" for s in PHASE_STEPS["Done"]}

    def _m(v):
        return "done" if v == "done" else ("fault" if v == "fault" else "cur")

    idf, pwr = _m(done.get("identify")), _m(done.get("power_off"))
    fin = "done" if idf == "done" and pwr == "done" else "pending"
    return {"identify": idf, "power-off": pwr, "done": fin}


def _record(slot, c, st, settings, live_link, macs):
    discover_steps = _discover_steps(c, st, live_link)
    discover_done = phase_advances(discover_steps)
    steps = {"Discover": discover_steps, "Firmware": _firmware_steps(st),
             "Qualify": _qualify_steps(st), "Done": _done_steps(st)}
    fw_gates = fw_gates_passed(st)
    qualify_done = phase_done(steps["Qualify"])
    # Completion latch (spec 2026-09-11): once the Done tail has recorded a
    # verdict, power and lease state no longer move the phase. Powering a
    # finished blade off used to flip Firmware's power-on and Discover's
    # host-pinged back to cur and drop a green tile to violet. Since the
    # 2026-09-12 ruling the Discover/Firmware checklists are held too, from
    # the snapshot the verdict took (done.steps): the bar paints a phase green
    # only from the run's own results, never from a phase index.
    verdict = (st.get("done") or {}).get("verdict")
    steps = _latched_steps(st, steps)
    if verdict == "pass":
        phase = "Done"
    elif verdict == "fail":
        phase = "Qualify"       # latched red: the failed step stays visible
    elif not discover_done:
        phase = "Discover"
    elif not fw_gates:
        phase = "Firmware"
    elif not qualify_done:
        phase = "Qualify"
    else:
        phase = "Done"
    return {
        "port": slot["port"], "switch": slot["switch"],
        "ou": slot["ou"], "height": slot["height"], "width": slot["width"],
        "col": slot["col"], "group": slot["group"],
        "serial": st.get("serial"),
        "bmc_mac": c.get("bmc_mac"), "host_mac": c.get("host_mac"),
        "bmc_ip": c.get("bmc_ip"), "host_ip": c.get("host_ip"),
        "bmc_leased": bool(c.get("bmc_leased")), "host_leased": bool(c.get("host_leased")),
        "power_on": st.get("power_on"), "watts": st.get("watts"),
        "bmc_pinged": bool(st.get("bmc_pinged")),
        "phase": phase,
        "step": None if verdict == "pass" else next(
            (n for n, s in discover_steps.items() if s not in _ADVANCES), None),
        "steps": steps,
        "step_notes": _step_notes(st),
        "fault_notes": _fault_notes(st),
        "ladder_view": _ladder_view(st),
        "ladder": st.get("ladder") or {},
        # The IPMI lane's uncontended human-reset stamp; the slot worker
        # reconciles its cached ladder against it (observe/worker.RealDeps).
        "ladder_reset_at": st.get("ladder_reset_at"),
        "verdict": verdict,
        "launch_at": st.get("launch_at"),
        "fw_gates": fw_gates,
        "run_id": (st.get("qual") or {}).get("run_id"),
        "order_no": st.get("order_no") or settings.get("order_no"),
        "population": settings.get("population"),
        "pop_override": st.get("pop_override"),
        "sdr": st.get("sdr") or {}, "sel": st.get("sel") or [],
        "alerts": st.get("alerts") or [],
        "updated_at": st.get("updated_at"),
        "fw": {"bmc": st.get("fw_bmc") or {}, "bios": st.get("fw_bios") or {}, "nic": st.get("fw_nic") or {}},
        "link": live_link,
        "macs_seen": [str(m).lower() for m in macs],
    }


_COLS_FOR_WIDTH = {1: ["full"], 2: ["L", "R"], 3: ["L", "C", "R"],
                   4: ["A", "B", "C", "D"]}


def _fill_placeholders(out: list) -> list:
    """Append ghost placeholder cells (port=None, empty+placeholder) for missing
    (group, ou, col) positions so each width-N group renders a complete N-column
    grid. Without this, a trimmed/absent column (e.g. a non-post port removed from
    a shared switch's rack) shifts the remaining cells left and mis-aligns L/C/R.
    Only fills columns for OU rows that already have at least one real cell."""
    present = {(s["group"], s["ou"], s["col"]) for s in out}
    groups: dict = {}
    for s in out:
        g = groups.setdefault(s["group"], {"width": s["width"], "switch": s["switch"],
                                           "height": s["height"], "ous": set()})
        g["ous"].add(s["ou"])
    ghosts = []
    for gid, info in groups.items():
        for ou in info["ous"]:
            for col in _COLS_FOR_WIDTH.get(info["width"], []):
                if (gid, ou, col) not in present:
                    ghosts.append({"port": None, "switch": info["switch"], "ou": ou,
                                   "height": info["height"], "width": info["width"],
                                   "col": col, "group": gid, "empty": True,
                                   "placeholder": True})
    return out + ghosts


def build_slots(slots, consumed, state, settings, switch_facts=None) -> list:
    """Full grid in rack order; populated slots are complete blade records.

    The live switch overlay (link, macs_seen) is applied to EVERY slot from
    switch_facts (arista-keyed), independent of whether a reservation exists.
    A slot is populated if it has a reservation, durable state, OR is link-up.
    """
    switch_facts = switch_facts or {}
    out = []
    for slot in slots:
        port = slot["port"]
        c = consumed.get(port)
        st = state.get(port) or {}
        fact = switch_facts.get(geometry.to_arista(port)) or {}
        live_link = _link_value(fact.get("link")) if fact else ((c or {}).get("link") or "nolink")
        macs = fact.get("macs") or []
        if not c and not st and live_link != "link":
            out.append({"port": port, "switch": slot["switch"], "ou": slot["ou"],
                        "height": slot["height"], "width": slot["width"],
                        "col": slot["col"], "group": slot["group"], "empty": True})
        else:
            out.append(_record(slot, c or {}, st, settings, live_link, macs))
    out = _fill_placeholders(out)
    out.sort(key=lambda s: (-(s["group"] or 0), -s["ou"], _COL_ORDER.get(s["col"], 0)))
    return out
