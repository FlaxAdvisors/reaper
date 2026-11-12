"""Bang-side, inside the triage api container: give a family's agent rows a fresh evaluation.

Standalone on purpose -- reaper pipes this file into `docker exec -i prod-api-1 python -`,
prefixed by a `SPEC = {...}` line and followed by `main(SPEC)`. It never imports
reapercli. It imports TRIAGE's own modules (cwd /home/app/api), so the rules for
"in flight", "held" and "power-on debt" are triage's, not a copy (spec 2026-11-11 §7).

Run it only while the agent is stopped (holder) or not running (standby). It writes
through the agent's own Store (in place, under its lock), after a full backup.
Prints one JSON object: {"rows": [...], "applied": bool, "backup": path|null}.
"""
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

# Rows at rest: deleting one makes the next scan judge the port as if just inserted
# (what bios Retry / bmc Clear do for one port). Any other phase is mid-drive: skip.
RESTING = {
    "bmc": ("needs_update", "up_to_date", "done", "oem", "target_mismatch", "facebook",
            "unmatched", "unresolved", "fault", "idle", "at_target", "off_target"),
    "bios": ("blocked", "done", "up_to_date", "fault", "idle", "unmatched", "unresolved",
             "at_target", "off_target"),
}
BIOS_DARK = ("needs_power_cycle", "chip_unrecoverable")   # a reset would reflash a dark host


def decide(lane: str, rec: dict, in_family, new_target: str, deps) -> tuple:
    phase = rec.get("phase") or ""
    port = rec.get("port") or ""
    if in_family is None:
        return "skip", "not in feed"
    if not in_family:
        return "keep", "other family"
    if (rec.get("target_version") or "") == new_target:
        return "keep", "on new target"
    # a bios-fw-hold pins the host dark for BOTH agents: bmc_fw powers a host off to flash
    if deps.held(port) or rec.get("hold_set") or phase == "held":
        return "skip", "operator hold"
    if lane == "bios":
        if deps.in_flight(rec):
            return "skip", "in flight"
        if phase in BIOS_DARK:
            return "skip", f"{phase}: retry from the tile if wanted"
        if phase == "confirmed":
            return "keep", "confirmed: bios_fw demotes it on a target move"
    else:
        if deps.claim_active(port) or rec.get("power_lock"):
            return "skip", "in flight"
        if rec.get("hold_power_on"):
            if phase == "fault" and deps.cleared_record(rec):
                return "repark", "fault with power-on debt: re-driven, not deleted"
            return "skip", "power-on debt"
    if phase in RESTING[lane]:
        return "delete", f"{phase} on old target"
    return "skip", f"phase {phase or '(none)'}: mid-drive"


def repark(rec: dict, new_target: str, deps) -> dict:
    out = dict(deps.cleared_record(rec))
    out["target_version"] = new_target
    return out


@dataclass
class Env:
    store: object                       # .all() -> dict, .remove(port), .put(port, rec)
    deps: object
    family_entry: dict
    match: Optional[Callable]           # feed row -> manifest entry (or None/{}); None on the standby
    feed: Optional[Callable]            # () -> {port: feed row}; None on the standby
    now: str
    clock: Callable = time.monotonic


class Deadline(Exception):
    """The caller's ssh timeout is near: write nothing (the agent may be running again)."""


def run(spec: dict, env: Env) -> dict:
    lane, new = spec["lane"], spec["new_target"]
    t0 = env.clock()
    rows = env.store.all()
    feed = env.feed() if env.feed else None
    only = set(spec["ports"]) if spec.get("ports") is not None else None
    out = []
    for port in sorted(rows):
        rec = dict(rows[port], port=port)
        if only is not None:
            in_family = port in only
        elif feed is None or port not in feed:
            in_family = None
        else:
            in_family = env.match(feed[port]) == env.family_entry
        act, why = decide(lane, rec, in_family, new, env.deps)
        out.append({"port": port, "phase": rec.get("phase") or "", "target": rec.get("target_version") or "",
                    "action": act, "reason": why})
    backup = None
    if spec.get("apply") and any(r["action"] in ("delete", "repark") for r in out):
        spent = env.clock() - t0
        if spec.get("deadline_s") and spent > spec["deadline_s"]:
            raise Deadline(f"{spent:.0f}s > deadline {spec['deadline_s']}s: nothing written")
        backup = f"{spec['store']}.reaperfw-{env.now}"
        shutil.copy2(spec["store"], backup)
        os.chmod(backup, 0o600)
        for r in out:
            if r["action"] == "delete":
                env.store.remove(r["port"])
            elif r["action"] == "repark":
                env.store.put(r["port"], repark(rows[r["port"]], new, env.deps))
    return {"rows": out, "applied": bool(spec.get("apply")), "backup": backup}


def manifest_data(spec: dict) -> dict:
    """The manifest to judge against: the NEW one passed in (a plan before the render),
    else the rendered file."""
    if spec.get("manifest_json") is not None:
        return spec["manifest_json"]
    with open(spec["manifest"]) as f:
        return json.load(f)


def _triage_env(spec: dict) -> Env:          # pragma: no cover -- runs in the api container
    sys.path.insert(0, os.getcwd())
    from types import SimpleNamespace
    data = manifest_data(spec)
    family_entry = data[spec["family"]]
    if family_entry.get("target_version") != spec["new_target"]:
        raise SystemExit(f"manifest says {family_entry.get('target_version')!r}, "
                         f"not {spec['new_target']!r}: render first")
    if spec["lane"] == "bmc":
        from bmcfw.manifest import Manifest
        from bmcfw.store import Claim, Store, cleared_record
        m = Manifest(data)
        match = lambda r: m.match(r.get("product_name"), r.get("bmc_kind"))  # noqa: E731
        from biosfw.hold import Hold
        deps = SimpleNamespace(in_flight=lambda rec: False, held=Hold().held,
                               claim_active=Claim().active, cleared_record=cleared_record)
    else:
        from biosfw.hold import Hold
        from biosfw.manifest import Manifest
        from biosfw.retry import in_flight
        from biosfw.store import Store
        m = Manifest(data)
        match = lambda r: m.match(r.get("product_name"))  # noqa: E731
        deps = SimpleNamespace(in_flight=in_flight, held=Hold().held,
                               claim_active=lambda p: False, cleared_record=lambda rec: None)
    feed = None
    if spec.get("feed_url") and spec.get("ports") is None:
        import requests
        base = spec["feed_url"].rstrip("/")

        def feed():
            from concurrent.futures import ThreadPoolExecutor
            ports = requests.get(base + "/api/v1/ports", timeout=10).json()
            with ThreadPoolExecutor(max_workers=16) as pool:   # ~50 ports: seconds, not minutes
                rows = pool.map(lambda p: requests.get(base + "/api/v1/port/" + p, timeout=10).json(), ports)
                return dict(zip(ports, rows))
    return Env(store=Store(spec["store"]), deps=deps, family_entry=family_entry, match=match,
               feed=feed, now=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))


def main(spec: dict) -> None:                # pragma: no cover
    try:
        print(json.dumps(run(spec, _triage_env(spec))))
    except Deadline as e:
        sys.exit(f"deadline: {e}")
