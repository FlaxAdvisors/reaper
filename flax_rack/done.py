"""Done stage for a rack order (spec section 7.4): the end summary, the boot reset and
the customer CSVs.

Per host-bearing device, in parallel (no lockstep: nothing here waits for the rack),
three rungs recorded with stage `done`:

  components   the latest Qualify inventory's macinv -v parsed into order.db's component
               rows (report.components_from_inventory, source_run = that inventory run);
               `skip` "no Qualify inventory" when there is none (its old rows are
               dropped: never another run's parts). A table of them is the artifact.
  end-summary  flax_rungs.endsummary.collect: SDR + SEL out of band (the verdict is the
               SEL's: a non-benign entry fails, SEL not read is `unmeasured`), and the
               installed OS in band only when the device's latest Deploy firstboot passed
               (else "no installed OS to reach": a device that never deployed is read
               out of band only). Raw text under artifacts/<pos>/done<run>/end-summary/.
  boot-reset   pxeboot.remove of the device's hostname iPXE file (a power-on must not
               reinstall) + redfish.clear_boot_override (target None): the node boots
               its own UEFI order. Both must succeed, and that order (this Done's
               end-summary efibootmgr -v) must not start with a network/PXE entry (the
               node would PXE-boot at the customer): a fail, the override still cleared.
               No efibootmgr capture (never deployed) is a row, not a fail.

A device with no Firmware result in the current pass is flagged: the event `done: no
Firmware result this pass: <pos ...>` and a `firmware` row + note on its components
rung (the CSV's version cells for it are blank: versions are this pass's reads).

Then report.write rewrites <customer>-<order>-devices.csv / -firmware.csv /
-inventory.csv in the order dir (every device, IOM and switch row; from each
row's own latest runs; as-built ports from the rack's live link monitor when it runs) and
the event `done: csv written (<n> devices)` is logged. A positions run re-reads those
devices only and rewrites all three files.

Done never power-controls a node: a customer rack ships powered as the operator leaves
it (donum 84's `nodepower off` is not carried over). The rack's Cancel is checked before
each device and between its rungs: what has not started is `skip` "cancelled", and a
cancelled run writes no CSV.

flax_rungs functions never touch order.db; this module records them. ctx.rungs (a
namespace like real_rungs()) replaces them in tests and in RACKPOST_FAKE.
"""
import csv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from flax_rungs import endsummary, pxeboot, redfish
from flax_rungs.result import RungResult

from . import report
from .addressing import plan_addresses
from .qualify import _artifact as _qartifact, _record as _qrecord
from .stages import cancelled, creds

STAGE = "done"
DONE_RUNGS = ("end-summary", "boot-reset", "components")
_ORDER = ("components", "end-summary", "boot-reset")    # the order a device walks them
MAX_PARALLEL = 8            # devices read at once (ipmitool + ssh per device)


def real_rungs():
    return SimpleNamespace(end_summary=endsummary.collect, pxe_remove=pxeboot.remove,
                           clear_override=redfish.clear_boot_override)


def done_rungs(d) -> list[str]:
    """The Done rungs of a device ([] = no host: nothing to summarise or reset)."""
    return list(DONE_RUNGS) if d.endpoints.get("hosts") else []


def _record(ctx, pos, rung, res: RungResult) -> RungResult:
    return _qrecord(ctx, pos, rung, res, stage=STAGE, seg_prefix="done")


def _rung(ctx, pos, rung, fn) -> RungResult:
    try:
        res = fn()
    except Exception as e:                  # noqa: BLE001 -- recorded, never raised
        res = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
    return _record(ctx, pos, rung, res)


# ---------------------------------------------------------------- rungs
NO_FIRMWARE = "no Firmware result this pass: the CSV's version cells are blank"


def has_firmware(lr, key) -> bool:
    """A Firmware row for key in the current pass (latest_runs is pass-scoped)."""
    return any(k[0] == key and r.get("stage") == "firmware" for k, r in lr.items())


def components(ctx, lr, pos) -> RungResult:
    res = _components(ctx, lr, pos)
    if has_firmware(lr, pos):
        return res
    d = dict(res.detail)
    d["rows"] = [*(d.get("rows") or []), ["firmware", NO_FIRMWARE]]
    d["notes"] = [*(d.get("notes") or []), "run Firmware, then Done again, to fill bmc/bios/"
                  "nic/backup versions"]
    return RungResult(res.verdict, d, res.artifacts)


def _components(ctx, lr, pos) -> RungResult:
    text = report.inventory_text(ctx.dir, lr, pos)
    if text is None:
        ctx.db.set_components(pos, [], 0)   # an older Done's rows are not this inventory's
        return RungResult("skip", {"reason": "no Qualify inventory",
                                   "headline": "no Qualify inventory (macinv -v)"})
    rid = ctx.db.latest_run_id(pos, report.INVENTORY_RUNG)
    rows = report.components_from_inventory(text)
    ctx.db.set_components(pos, rows, rid)
    if not rows:
        why = "no component parsed from the inventory's macinv -v"
        return RungResult("fail", {"error": why, "headline": why, "source_run": rid})
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    no_sn = sum(1 for r in rows if not r["serial"])
    table = "".join(f"{r['kind']:5s} {r['slot'] or '-':24s} {r['serial'] or '-':24s} {r['model']}\n"
                    for r in rows)
    return RungResult("pass", {
        "headline": ", ".join(f"{n} {k}" for k, n in kinds.items()), "source_run": rid,
        "rows": [["source", f"Qualify inventory run {rid} (macinv-v)"],
                 *[[k, str(n)] for k, n in kinds.items()],
                 ["no serial", str(no_sn)]],
        "notes": ["the customer component CSV's rows for this device"]},
        {"components.txt": "kind  slot                     serial                   model\n"
                           + table})


def end_summary(ctx, R, cr, lr, x) -> RungResult:
    fb = lr.get((x["pos"], "firstboot")) or {}
    host_ip = x["host_ip"] if fb.get("verdict") == "pass" else None
    no_user = bool(host_ip) and not cr.host
    user, pw = cr.host if cr.host else ("", "")
    res = R.end_summary(None if no_user else host_ip, x["bmc_ip"], host_user=user,
                        host_pw=pw, bmc_creds=cr.bmc, runner=ctx.runner)
    if no_user:                             # an installed OS, but no login to read it with
        det = dict(res.detail)
        det["rows"] = [["in-band", "skipped: add credentials-host.json (flax row)"]
                       if r[0] == "in-band" else r for r in det.get("rows") or []]
        res = RungResult(res.verdict, det, res.artifacts)
    return res


def boot_reset(ctx, R, cr, x) -> RungResult:
    name = x["hostname"]
    if name:
        gone, err = R.pxe_remove(name)
    else:
        gone, err = False, "no hostname in the address plan"
    ov = R.clear_override(x["bmc_ip"], cr.redfish, runner=ctx.runner)
    ovd = ov.detail or {}
    rows = [["hostname file", f"hostname-{name}.ipxe gone" if gone else f"NOT removed: {err}"],
            ["boot override", f"None (redfish, as {ovd.get('user')})" if ov.verdict == "pass"
             else f"NOT cleared: {ovd.get('error') or ov.verdict}"]]
    # the persistent UEFI order the node falls back to once the override is gone: this
    # Done run's end-summary efibootmgr -v (in band; none for a node that never deployed)
    text = _qartifact(ctx, ctx.db.latest_runs(), x["pos"], "end-summary", "bootorder")
    bid, line, net = endsummary.first_boot(text or "")
    if bid is None:
        rows.append(["boot order", "not read (no in-band efibootmgr -v this Done)"])
    elif net:
        rows.append(["boot order", f"NETWORK FIRST: {line}: the node would PXE-boot at "
                                   "the customer"])
    else:
        rows.append(["boot order", f"first {line or 'Boot' + bid + ' (no entry line)'}"])
    notes = ["no power action: the node stays as the operator left it"]
    bad = [f"{k} {v}" for k, v in rows if v.startswith(("NOT ", "NETWORK FIRST"))]
    if bad:
        why = "; ".join(bad)
        return RungResult("fail", {"error": why, "headline": why, "rows": rows, "notes": notes})
    return RungResult("pass", {"headline": "hostname file gone, boot override None",
                               "rows": rows, "notes": notes})


# ---------------------------------------------------------------- stage
def _device(ctx, R, cr, lr, x) -> dict:
    out = {}
    for rung in _ORDER:
        if cancelled(ctx):
            out[rung] = _record(ctx, x["pos"], rung,
                                RungResult("skip", {"reason": "cancelled"})).verdict
            continue
        if rung == "components":
            r = _rung(ctx, x["pos"], rung, lambda: components(ctx, lr, x["pos"]))
        elif rung == "end-summary":
            r = _rung(ctx, x["pos"], rung, lambda: end_summary(ctx, R, cr, lr, x))
        else:
            r = _rung(ctx, x["pos"], rung, lambda: boot_reset(ctx, R, cr, x))
        out[rung] = r.verdict
    return out


def targets(g, positions=None) -> list:
    """The host-bearing devices Done runs on (all, or `positions`). ValueError names a
    position that is not one."""
    hosts = [d for d in g.devices if d.endpoints.get("hosts")]
    if positions:
        bad = sorted(set(positions) - {d.pos for d in hosts})
        if bad:
            raise ValueError(f"not a host-bearing position: {', '.join(bad)}")
        hosts = [d for d in hosts if d.pos in positions]
    if not hosts:
        raise ValueError("no host-bearing device")
    return hosts


def run(ctx, positions: list[str] | None = None) -> dict:
    """Done for the rack (or `positions`). -> {"devices": {pos: {rung: verdict}},
    "csv": {"devices": path, "firmware": path, "inventory": path} | None (cancelled)}."""
    if ctx.db.closed_at():
        raise RuntimeError("order is closed")
    g = ctx.g
    devs = targets(g, positions)
    cr = creds(ctx)
    R = ctx.rungs or real_rungs()
    xs = {d.pos: {"pos": d.pos, "bmc_ip": None, "host_ip": None, "hostname": None}
          for d in devs}
    for a in plan_addresses(g):
        x = xs.get(a.pos)
        if x is None:
            continue
        if a.role == "bmc":
            x["bmc_ip"] = a.ip
        elif a.role == "host":              # = the Kea reservation's (and iPXE file's) name
            x["host_ip"], x["hostname"] = a.ip, a.name
    ctx.db.add_event("done", "done started: " + ", ".join(xs))
    ctx.db.mark_stage_start("done", list(xs))
    lr = ctx.db.latest_runs()
    nofw = [p for p in xs if not has_firmware(lr, p)]
    if nofw:
        ctx.db.add_event("done", "done: no Firmware result this pass: " + ", ".join(nofw)
                         + " (CSV version cells blank)")

    def one(x):
        try:
            return x["pos"], _device(ctx, R, cr, lr, x)
        except Exception as e:              # noqa: BLE001 -- recording itself failed
            ctx.db.add_event("error", f"done {x['pos']}: {type(e).__name__}: {e}")
            return x["pos"], {}

    with ThreadPoolExecutor(max_workers=max(1, min(MAX_PARALLEL, len(xs))),
                            thread_name_prefix=f"done-{g.url_prefix}") as ex:
        verdicts = dict(ex.map(one, xs.values()))
    bad = {p: [r for r, v in vs.items() if v in ("fail", "unmeasured")]
           for p, vs in verdicts.items()}
    bad = {p: rs for p, rs in bad.items() if rs}
    clean = ", ".join(p for p in verdicts if p not in bad) or "none"
    dirty = ", ".join(f"{p} ({' '.join(rs)})" for p, rs in bad.items())
    ctx.db.add_event("done", ("done cancelled" if cancelled(ctx) else "done finished")
                     + f"; clean: {clean}" + (f"; not clean: {dirty}" if bad else ""))
    if cancelled(ctx):
        ctx.db.add_event("done", "csv not written (cancelled)")
        return {"devices": verdicts, "csv": None}
    lm = getattr(ctx, "links", None)        # the rack's LinkPoller: as-built ports
    paths = report.write(ctx.dir, g, ctx.db, *((lm.snapshots(), lm.clock()) if lm else ()))
    with open(paths["devices"], newline="") as f:
        n = sum(1 for _ in csv.reader(f)) - 1
    ctx.db.add_event("done", f"csv written ({n} devices): "
                             + ", ".join(Path(p).name for p in paths.values()))
    return {"devices": verdicts, "csv": {k: str(p) for k, p in paths.items()}}
