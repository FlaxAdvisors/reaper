"""Per-step detail for the tile's step modal (ruling 2026-09-12): not a
pass/fail word but the DATA an operator needs to see why a step failed, or the
good numbers behind a pass. Pure: `detail(record, row, phase, step)` reads the
blade record (blades.build_slots output) plus the raw post_state row (the
agent's per-stage summaries and timings live only there) and returns

    {"phase", "step", "status", "headline", "rows": [[label, value], ...],
     "notes": [text, ...]}

rendered generically by rack.html. Rows are short label/value pairs; notes
are sentences (fault reasons, provenance, what a status means here).
"""
import time

from . import blades

_STATUS_WORD = {"done": "passed", "cur": "in progress", "pending": "not reached",
                "fault": "FAILED", "skip": "skipped (not applicable)",
                "unknown": "no evidence collected"}


def _t(epoch) -> str:
    """UTC wall-clock for an epoch, '' for None."""
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(epoch))


def _dur(a, b) -> str:
    if not a or not b:
        return ""
    s = int(round(b - a))
    return "%dm%02ds" % divmod(s, 60) if s >= 60 else "%ds" % s


def _yn(v) -> str:
    return "yes" if v else "no"


def _fw_rows(slice_, frozen_state):
    s = slice_ or {}
    rows = [["current", s.get("current") or s.get("current_version") or "not read"],
            ["target", s.get("target") or s.get("target_version") or "unknown"],
            ["daemon phase", s.get("phase") or "not evaluated"],
            ["mode", s.get("mode") or "detect"]]
    if s.get("fault_reason"):
        rows.append(["reason", s["fault_reason"]])
    return rows


_PHASE_MEANING = {
    "up_to_date": "current version equals the manifest target: nothing to flash",
    "done": "flashed this run and re-read at the target version",
    "needs_update": "current version differs from the target; in detect mode nothing is flashed, so this step stays open until triage flashes it",
    "checking": "version read in progress",
    "flashing": "flash in progress", "monitoring": "flash in progress (monitoring)",
    "activating": "new image activating (BMC reboot)",
    "fault": "the daemon recorded a fault (see reason)",
    "unreachable": "the daemon could not reach the device on its last pass (BMC off the network, or the host is off)",
    "unknown": "version unreadable on either side",
    "oem": "OEM board: firmware is not flax-managed",
    "unsupported": "platform not in the manifest: nothing to flash",
}


def _discover(rec, row, step, status):
    lad = rec.get("ladder") or {}
    lv = rec.get("ladder_view") or {}
    marks = lv.get("marks") or {}
    p_on = lv.get("power_on_at")
    rows, notes = [], []
    if step == "switchportlink":
        rows = [["link", rec.get("link") or "unknown"], ["switch port", "%s %s" % (rec.get("switch"), rec.get("port"))],
                ["MACs on wire", ", ".join(rec.get("macs_seen") or []) or "none"]]
    elif step in ("bmc-mac-seen", "host-mac-seen"):
        kind = step.split("-")[0]
        mac = rec.get(kind + "_mac")
        rows = [[kind.upper() + " MAC", mac or "unknown"],
                ["seen on this port", _yn(mac and mac.lower() in (rec.get("macs_seen") or []))],
                ["MACs on wire", ", ".join(rec.get("macs_seen") or []) or "none"]]
    elif step in ("bmc-reserved", "host-reserved"):
        kind = step.split("-")[0]
        rows = [[kind.upper() + " MAC", rec.get(kind + "_mac") or "unknown"],
                ["reservation IP", rec.get(kind + "_ip") or "none"]]
        notes.append("a DHCP reservation is written by reconcile once the MAC is seen on a post port")
    elif step in ("bmc-leased", "host-leased"):
        kind = step.split("-")[0]
        rows = [[kind.upper() + " IP", rec.get(kind + "_ip") or "none"],
                ["active lease", _yn(rec.get(kind + "_leased"))]]
        if kind == "host" and status in ("cur", "pending"):
            notes.append("the host lease appears only while the live ISO is up; it expires after power-off")
    elif step == "bmc-pinged":
        rows = [["BMC IP", rec.get("bmc_ip") or "none"], ["ping", _yn(rec.get("bmc_pinged"))],
                ["chassis power", rec.get("power_on") or "unreadable"], ["draw", rec.get("watts") or "n/a"]]
    elif step == "serial":
        rows = [["serial", rec.get("serial") or "not read"]]
        notes.append("product serial from the BMC FRU (ipmitool fru); read by the IPMI lane once the BMC answers")
    elif step == "power-on":
        rows = [["chassis power", rec.get("power_on") or "unreadable"],
                ["engine power-on at", _t(p_on) or "not issued"],
                ["last attempt", _t(lad.get("last_power_attempt")) or "none"],
                ["awaiting power reading", _yn(lad.get("power_on_pending"))]]
        if lad.get("human_power_on"):
            notes.append("powered on by an operator (the lane saw off to on without an engine request)")
        if status == "cur" and rec.get("verdict"):
            notes.append("a latched blade is not powered on again by the engine; power it on by hand to re-run")
    elif step in blades.BOOT_MARKER_RUNGS:
        key = {"tftp-seen": "tftp", "ipxe-seen": "ipxe", "live-iso-seen": "iso"}[step]
        src = {"tftp": "dnsmasq log (TFTP request from the host IP)",
               "ipxe": "nginx access log (iPXE fetched its script)",
               "iso": "nginx access log (live ISO fetched)"}[key]
        mark = marks.get(key)
        rows = [["seen at", _t(mark) or "not seen"],
                ["after power-on", ("+" + _dur(p_on, mark)) if (mark and p_on) else ""],
                ["evidence", src], ["budget", "%s s" % blades.ladder_budget_s(step)]]
        if status == "unknown":
            notes.append("the blade was already on when the worker first looked (observe restart, fault cleared by hand, "
                         "or an operator power-on): this boot's markers were never collected. Power-cycle to collect them.")
    elif step == "bmc-ready":
        fru = lad.get("bmc_fru") or {}
        mark = marks.get("bmcready")
        rows = [["BMC IP", rec.get("bmc_ip") or "none"], ["BMC ping", _yn(rec.get("bmc_pinged"))],
                ["data read at", _t(mark) or "not yet"],
                ["after power-on", ("+" + _dur(p_on, mark)) if (mark and p_on) else ""],
                ["board", ("%s %s" % (fru.get("board_mfg", ""), fru.get("product", ""))).strip() or "not read"],
                ["FRU serial", fru.get("serial") or "not read"],
                ["budget", "%s s" % blades.ladder_budget_s(step)]]
        notes.append("the BMC goes dark for 60-100 s after the chassis power-on and comes back in stages (ping before IPMI); "
                     "nothing that reads it (firmware probe, the agent's FRU/SEL/SDR stages, the population rules) starts before "
                     "it answers `ipmitool fru` with a board manufacturer and a serial")
    elif step in ("host-pinged", "host-ssh"):
        key = "ping" if step == "host-pinged" else "ssh"
        mark = marks.get(key)
        rows = [["host IP", rec.get("host_ip") or "none"], ["seen at", _t(mark) or "not yet"],
                ["after power-on", ("+" + _dur(p_on, mark)) if (mark and p_on) else ""],
                ["budget", "%s s" % blades.ladder_budget_s(step)]]
        if step == "host-ssh":
            notes.append("ssh as the live ISO's host user; iPXE answers ping long before the kernel, so this budget is the long one")
    rows = [r for r in rows if r[1] != ""]
    if lv.get("rung"):
        rows.append(["ladder", "%s since %s" % (lv["rung"], _t(lv.get("since")))])
    return rows, notes


def _firmware(rec, row, step, status):
    fw = rec.get("fw") or {}
    key = {"bmc": "bmc", "bios": "bios", "mlx": "nic"}[step.split("-")[0]]
    s = fw.get(key) or {}
    rows, notes = [], []
    if key == "nic":
        devs = s.get("devices") or []
        rows = [["daemon phase", s.get("phase") or "not evaluated"], ["mode", s.get("mode") or "detect"],
                ["cards", str(len(devs))]]
        for d in devs:
            rows.append(["card %s" % d.get("pci", "?"),
                         "%s  %s -> %s  [%s]" % (d.get("psid", "?"), d.get("current") or "?",
                                               d.get("target") or "?", d.get("phase") or "?")])
        if s.get("fault_reason"):
            rows.append(["reason", s["fault_reason"]])
    else:
        rows = _fw_rows(s, status)
    meaning = _PHASE_MEANING.get(s.get("phase"))
    if meaning:
        notes.append(meaning)
    probe = ((rec.get("ladder") or {}).get("probe_calls") or {}).get({"bmc": "fwd", "bios": "biosd", "nic": "nicd"}[key])
    if probe:
        rows.append(["last on-demand probe", _t(probe)])
    frozen = ((row.get("done") or {}).get("steps") or {}).get("Firmware") if rec.get("verdict") else None
    if frozen is not None:
        notes.append("status frozen with the verdict (the live daemon row above may have moved since; e.g. `unreachable` after power-off)")
    return rows, notes


def _qualify(rec, row, step, status):
    q = row.get("qual") or {}
    qs = (q.get("steps") or {}).get(step) or {}
    rows, notes = [], []
    if step == "agent-reachable":
        ag = q.get("agent") or {}
        rows = [["host IP", rec.get("host_ip") or "none"], ["agent reachable", _yn(ag.get("reachable"))],
                ["agent version", ag.get("ver") or "unknown"], ["run", q.get("run_id") or "none"],
                ["launched at", _t(rec.get("launch_at")) or "not launched"]]
        notes.append("the engine ssh-starts the agent (post.sh postautomate) once the firmware gate passes; it must answer /health within %s s" % blades.ladder_budget_s("agent-reachable"))
        return rows, notes
    if step == "population-check":
        pop = row.get("pop") or {}
        rows = [["profile", pop.get("profile") or "none in effect"], ["verdict", pop.get("verdict") or "not evaluated"]]
        for r in pop.get("failed_rules") or []:
            rows.append(["missing", r])
        notes.append("rules are matched against this run's inventory digest (macinv count form); the POP button shows the full rule list")
        if pop.get("verdict") == "red":
            notes.append("a red population fails the run: the blade goes back to triage, the profile is not relaxed")
        return rows, notes
    if step == "console":
        rows = [["capture", "sol.txt artifact for this run" if status == "done" else "none"]]
        if qs.get("summary", {}).get("reason"):
            rows.append(["reason", qs["summary"]["reason"]])
        notes.append("SOL capture from the power-on mark to the verdict; the SOL button shows it live")
        return rows, notes
    rows = [["agent status", qs.get("status") or "not reported"]]
    if qs.get("started"):
        rows += [["started", _t(qs["started"])], ["ended", _t(qs.get("ended")) or "running"],
                 ["duration", _dur(qs["started"], qs.get("ended"))]]
    for k, v in (qs.get("summary") or {}).items():
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v) or "none"
        elif isinstance(v, dict):
            v = ", ".join("%s=%s" % kv for kv in v.items()) or "none"
        rows.append([str(k), str(v)])
    rows = [r for r in rows if r[1] != ""]
    hint = _QUAL_HINTS.get(step)
    if hint:
        notes.append(hint)
    arts = [a.get("name") for a in (qs.get("artifacts") or []) if a.get("name")]
    if arts:
        rows.append(["artifacts", ", ".join(arts)])
    return rows, notes


_QUAL_HINTS = {
    "sdr-pre": "sensor readings and thresholds before stress; `lines` is the SDR row count (compare with sdr-post)",
    "sdr-post": "sensor readings after stress; the SDR button shows both captures side by side",
    "sel-pre": "SEL before the run; fails only on CATERR. `unique_msgs` is the count of distinct event texts",
    "sel-clear": "the SEL is cleared so sel-post only sees this run's events",
    "sel-post": "SEL after stress; CATERR here fails the run. Expect `unique_msgs` near 1 (the clear marker)",
    "tooling": "the battery's tools are present on the live ISO",
    "inventory": "macinv/dmidecode/lspci/... dumps; `tools` is the number of artifacts captured",
    "fio": "disk exercise on physical media; skipped with `no storage` on a diskless blade",
    "iperf": "network throughput against the bang's iperf server; `attempts` counts server-busy retries",
    "mem-pre": "EDAC correctable/uncorrectable error counters before stress (ce/ue must be 0)",
    "cpu-mem-stress": "stress on every core plus most of RAM for the configured timeout; fails on a non-zero exit",
    "mem-post": "EDAC counters after stress; any new ce/ue here is a DIMM fault",
}


def _done(rec, row, step, status):
    d = row.get("done") or {}
    rows, notes = [], []
    if step == "identify":
        rows = [["result", d.get("identify") or "not run"]]
        notes.append("chassis identify LED forced on: the blade is finished, pull it")
    elif step == "power-off":
        rows = [["result", d.get("power_off") or "not run"], ["chassis power now", rec.get("power_on") or "unreadable"]]
        if d.get("power_off_reason"):
            rows.append(["reason", d["power_off_reason"]])
        notes.append("the engine powers the blade off and reads the chassis back for up to 30 s; only a read `off` passes")
    elif step == "done":
        holes = rec.get("holes") or {}
        rows = [["verdict", d.get("verdict") or "none"], ["run", (row.get("qual") or {}).get("run_id") or "none"],
                ["run clean", _yn(rec.get("clean"))]]
        for p, names in holes.items():
            rows.append(["open in " + p, ", ".join(names)])
        notes.append("the clean mark: completes only when identify and power-off succeeded AND every earlier step passed or was legitimately skipped; "
                     "a pass with open steps is finished but NOT ready to ship (re-run, or triage)")
        notes.append("a pass records the run into post_node (fleet viewer node page); a fail leaves the blade powered for inspection")
    return rows, notes


def detail(rec: dict, row: dict, phase: str, step: str) -> dict:
    steps = (rec.get("steps") or {}).get(phase) or {}
    status = steps.get(step, "pending")
    fn = {"Discover": _discover, "Firmware": _firmware, "Qualify": _qualify, "Done": _done}.get(phase)
    rows, notes = fn(rec, row or {}, step, status) if fn else ([], [])
    note = (rec.get("fault_notes") or {}).get(step)
    if note and status == "fault":
        notes.insert(0, note)
    elif note:
        notes.append(note)
    sn = (rec.get("step_notes") or {}).get(step)
    if sn == "added since this run":
        notes.insert(0, "this step was added to the pipeline after this run was recorded, so it has no result here; "
                        "the next power-on runs the whole pipeline including it")
    elif sn:
        notes.insert(0, sn)
    return {"phase": phase, "step": step, "status": status,
            "headline": "%s: %s" % (step, _STATUS_WORD.get(status, status)),
            "rows": rows, "notes": notes}
