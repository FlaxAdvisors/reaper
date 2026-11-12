"""The Qualify smart-health stage's evidence summary: per disk fio would read, the SMART
verdict the post.tgz agent judged (qual_stages.smart_health: ok | warn | failed, Triage's
Inventory health rules) with its reasons, plus the enclosure slot from the inventory's
alldisks. One failed disk fails the stage (the agent's verdict, never this module's);
fio then leaves the failed disks out. warn passes and stays in fio."""
import re

COLS = ["device", "location", "model", "serial", "SMART", "power-on h", "reasons"]
NOTES = ["smartctl --all on every unmounted nvme/sd disk (the disks fio reads); failed = "
         "self-assessment not PASSED/OK or unreadable, reallocated / pending / uncorrectable "
         "sectors, NVMe media errors or critical warning, SAS grown defects, spare at/below "
         "threshold; warn = >= 80% endurance used or spare near threshold",
         "a failed disk fails this stage and is left out of fio (fio's verdict is the tested "
         "disks' alone); a warn disk passes and stays in fio"]
_SLOT = re.compile(r"[A-Za-z]\w*_\w+")


def slots(alldisks: str | None) -> dict[str, str]:
    """dev -> where the disk sits, from alldisks: the enclosure slot label (4th column,
    e.g. Rear_ArrayDevice02), else the onboard port (`SATA port 0:0` on ahci, `NVMe
    nvme0`). A line with neither gives nothing (shown as —, never guessed)."""
    out = {}
    for ln in (alldisks or "").splitlines():
        f = ln.split()
        if len(f) < 4 or not f[0].startswith("/dev/"):
            continue
        dev = f[0][5:]
        if _SLOT.fullmatch(f[3]):
            out[dev] = f[3]
        elif len(f) > 4 and f[4] == "ahci" and re.fullmatch(r"\d+:\d+", f[3]):
            out[dev] = f"SATA port {f[3]}"
        elif dev.startswith("nvme") and re.fullmatch(r"\[nvme\d+\]", f[1]):
            out[dev] = f"NVMe {f[1][1:-1]}"
    return out


def _who(d: dict, slot: dict) -> str:
    s = slot.get(d.get("dev"))
    return f"{d.get('serial') or d.get('dev')}" + (f" ({s})" if s else "")


def summarize(summary: dict | None, alldisks: str | None = None) -> dict:
    """{"table", "rows", "notes", "headline"} for the rung's detail."""
    devs = [d for d in (summary or {}).get("devices") or [] if isinstance(d, dict)]
    slot = slots(alldisks)
    rows = [[d.get("dev") or "—", slot.get(d.get("dev")) or "—", d.get("model") or "—",
             d.get("serial") or "—", d.get("verdict") or "—",
             "—" if d.get("power_on_hours") is None else str(d["power_on_hours"]),
             "; ".join(d.get("reasons") or [])] for d in devs]
    bad = [d for d in devs if d.get("verdict") == "failed"]
    warn = [d for d in devs if d.get("verdict") == "warn"]
    head = None
    if devs:
        head = f"{len(devs)} drive{'s' if len(devs) != 1 else ''} · " + (
            f"{len(bad)} failed: " + ", ".join(_who(d, slot) for d in bad) if bad
            else "SMART ok") + (f" · {len(warn)} warn" if warn else "")
    srows = [[v, f"{d.get('dev')} {_who(d, slot)}: {'; '.join(d.get('reasons') or [])}"]
             for v, ds in (("failed", bad), ("warn", warn)) for d in ds]
    return {"table": {"cols": list(COLS), "rows": rows}, "rows": srows,
            "notes": list(NOTES), "headline": head}
