"""Build complete blade records: consumed (switch_facts/kea) ⋈ state ⋈ geometry.

Output is the full slot grid in rack order so the UI renders empties; populated
slots are complete, well-shaped records. Each phase's steps are driven by its
producer's post_state.vars slice: Discover from consumed switch/kea facts,
Firmware from vars.fw_bmc/fw_bios/fw_nic + power_on, Qualify from vars.qual
(the host_qual poller) + vars.pop, Done from vars.done. Phase advances
Discover -> Firmware -> Qualify -> Done as each phase's steps all reach 'done'.
"""
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

DISCOVER_STEPS = (
    "switchportlink", "bmc-mac-seen", "bmc-reserved", "bmc-leased", "bmc-pinged",
    "serial", "host-mac-seen", "host-reserved", "host-leased", "host-pinged",
)
PHASE_STEPS = {
    "Discover": DISCOVER_STEPS,
    "Firmware": ("power-read", "power-on", "bmc-checked", "bmc-updated",
                 "bios-checked", "bios-updated", "mlx-checked", "mlx-updated"),
    "Qualify": ("pxe-in-logs", "live-iso-in-logs", "agent-reachable",
                "sdr-pre", "sel-pre", "sel-clear", "tooling", "inventory", "fio",
                "population-check", "iperf", "mem-pre", "cpu-mem-stress",
                "mem-post", "sdr-post", "sel-post"),
    "Done": ("identify", "power-off", "done"),
}
_COL_ORDER = {"L": 0, "C": 1, "R": 2, "A": 0, "B": 1, "D": 3, "full": 0}


def _discover_flags(c: dict, st: dict, live_link):
    return [
        ("switchportlink", live_link == "link"),
        ("bmc-mac-seen", bool(c.get("bmc_mac_seen"))),
        ("bmc-reserved", bool(c.get("bmc_reserved"))),
        ("bmc-leased", bool(c.get("bmc_leased"))),
        ("bmc-pinged", bool(st.get("bmc_pinged"))),
        ("serial", bool(st.get("serial"))),
        ("host-mac-seen", bool(c.get("host_mac_seen"))),
        ("host-reserved", bool(c.get("host_reserved"))),
        ("host-leased", bool(c.get("host_leased"))),
        ("host-pinged", bool(st.get("host_pinged"))),
    ]


def _statuses(flags):
    out, seen_cur = {}, False
    for name, done in flags:
        if seen_cur:
            out[name] = "pending"
        elif done:
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


def _firmware_steps(st):
    """Explicit done|cur|pending|fault per Firmware step from power_on + fw_bmc
    + fw_bios + fw_nic. BMC check/update precede BIOS (BIOS needs a node boot
    -> lags; keep it last so progress has no mid-row gap); NIC follows BIOS.
    Key order matches PHASE_STEPS['Firmware'] because the UI renders steps in
    dict-key order."""
    power_on = st.get("power_on")
    read_done = power_on in ("on", "off")   # a definite power read; "unknown"/None = not yet
    bmc = st.get("fw_bmc")
    phase = (bmc or {}).get("phase")
    chk, upd = _FW_BMC.get(phase, ("pending", "pending"))
    bios = st.get("fw_bios")
    bchk, bupd = _FW_BIOS.get((bios or {}).get("phase"), ("pending", "pending"))
    mchk, mupd = _nic_steps(st)
    return {
        "power-read": "done" if read_done else "cur",
        "power-on": "done" if power_on == "on" else ("cur" if power_on == "off" else "pending"),
        "bmc-checked": chk, "bmc-updated": upd,
        "bios-checked": bchk, "bios-updated": bupd,
        "mlx-checked": mchk, "mlx-updated": mupd,
    }


_QUAL_MAP = {"pass": "done", "running": "cur", "pending": "pending",
             "fail": "fault", "skip": "done"}


def _qualify_steps(st):
    """Per-step done|cur|pending|fault for Qualify, from post_state.vars.qual.steps.
    Missing step -> pending (producer hasn't reached it)."""
    qsteps = (st.get("qual") or {}).get("steps") or {}
    out = {}
    for name in PHASE_STEPS["Qualify"]:
        status = (qsteps.get(name) or {}).get("status", "pending")
        out[name] = _QUAL_MAP.get(status, "pending")
    return out


def _done_steps(st):
    """identify -> power-off -> done, from post_state.vars.done. No verdict -> all
    pending; a fail verdict leaves them pending (node stays powered)."""
    done = st.get("done") or {}
    if done.get("verdict") != "pass":
        return {s: "pending" for s in PHASE_STEPS["Done"]}
    idf = "done" if done.get("identify") == "done" else "cur"
    pwr = "done" if done.get("power_off") == "done" else "cur"
    fin = "done" if idf == "done" and pwr == "done" else "pending"
    return {"identify": idf, "power-off": pwr, "done": fin}


def _record(slot, c, st, settings, live_link, macs):
    flags = _discover_flags(c, st, live_link)
    discover_steps = _statuses(flags)
    discover_done = all(d for _, d in flags)
    steps = {"Discover": discover_steps, "Firmware": _firmware_steps(st),
             "Qualify": _qualify_steps(st), "Done": _done_steps(st)}
    firmware_done = all(v == "done" for v in steps["Firmware"].values())
    qualify_done = all(v == "done" for v in steps["Qualify"].values())
    if not discover_done:
        phase = "Discover"
    elif not firmware_done:
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
        "step": next((n for n, d in flags if not d), None),
        "steps": steps,
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
