"""Stage orchestration for a rack order: the shared context and rung recorder; Discover
itself is discover_ladder (Post's 16-step ladder per device).

Close first writes the internal process report (flax_rack.process_report: every
attempt of every pass; a failure to write it fails Close, the order stays open), then
archives the rack (flax-control's rackpost.* tables) and then deletes the order's
Kea rows, both through flax-control; the order is marked closed only after both
succeed, so a failure leaves it open, reservations kept, and Close re-runnable (a
first close whose release fails withdraws the archive it just wrote). Then the Deploy
stage's per-run ansible inventory (the live hosts' IPs) leaves the order dir (best
effort: an `error` event if it cannot).
Start fresh (start_fresh) opens a new pass: earlier runs and waivers leave the tiles,
gates and CSVs (order.db scopes every latest read to the pass); nothing is deleted, the
customer CSVs of the ended pass are kept as <customer>-<order>-<kind>.pass<N>.csv.
Close needs Kea only: `creds` may be a zero-arg loader, called the first time a
Discover rung needs the switch or BMC login, so missing creds never block Close.
"""
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from flax_rungs.ansible import INVENTORY
from flax_rungs.result import RungResult

from .creds import Creds
from .geometry import Geometry
from .kea_client import KeaClient
from .orderdb import OrderDB

STAGE = "discover"


@dataclass
class OrderContext:
    dir: Path
    g: Geometry
    db: OrderDB
    kea: KeaClient
    creds: Creds | Callable[[], Creds]
    runner: object = field(default=subprocess.run)
    rungs: object = None        # Discover's + Qualify's rung functions (None = the real ones)
    cancel: threading.Event = field(default_factory=threading.Event)   # the rack's Cancel
    links: object = None        # the rack's LinkPoller (live switch snapshots); None = read here


def source(ctx) -> str:
    return ctx.g.kea_source


def creds(ctx) -> Creds:
    """The switch/BMC logins, loaded on first use (raises CredsError)."""
    if callable(ctx.creds):
        ctx.creds = ctx.creds()
    return ctx.creds


def cancelled(ctx) -> bool:
    """The operator pressed Cancel: stage loops stop between steps/phases (a power
    action already sent is never interrupted)."""
    c = getattr(ctx, "cancel", None)
    return bool(c is not None and c.is_set())


def _rung(ctx, pos, rung, fn, stage=STAGE) -> RungResult:
    rid = ctx.db.start_run(pos, stage, rung)
    try:
        r = fn()
    except Exception as e:           # a rung never leaves its row 'running'
        r = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
    ctx.db.finish_run(rid, r)
    return r


def run_discover(ctx, *, positions: list[str] | None = None) -> None:
    """Loose-node Post's 16-step Discover ladder per device (discover_ladder)."""
    from .discover_ladder import run_discover as _run
    _run(ctx, positions=positions)


def start_fresh(order_dir, db, who: str, reason: str) -> dict:
    """A new pass (the caller holds the rack's stage lock). The ended pass's customer
    CSVs are renamed *.pass<N-1>.csv, so Done's links and ✓ start over too.
    -> {"pass": the new pass, "renamed": [new file names]}."""
    from . import report
    p = db.start_pass(who, reason)
    renamed = []
    for src in sorted(Path(order_dir).iterdir()):
        if src.is_file() and report.CSV_RE.fullmatch(src.name):
            dst = src.with_name(f"{src.stem}.pass{p['id'] - 1}{src.suffix}")
            src.replace(dst)
            renamed.append(dst.name)
    db.add_event("fresh", f"start fresh (pass {p['id']}) by {who}: {reason}"
                          + (f"; kept {', '.join(renamed)}" if renamed else ""))
    return {"pass": p, "renamed": renamed}


def _retract(ctx, p) -> str:
    """Undo a first close's archive write (the same route, no rows: flax-control deletes
    this (order, rack)'s rows and inserts none). -> the event's clause."""
    try:
        ctx.kea.archive({**p, "devices": [], "macs": [], "components": [], "rungs": []})
        return "archive withdrawn"
    except Exception as e:                     # noqa: BLE001 -- reported, the order stays open
        return (f"archive NOT withdrawn ({type(e).__name__}: {e}): the archive lists the "
                "order closed until Close is re-run")


def close_order(ctx) -> dict:
    """Close -> {"released": n reservations, "archived": flax-control's row counts}.

    The archive is written first and carries the close time; if releasing the
    reservations then fails, a FIRST close withdraws that archive again (so the archive
    never shows an order closed while it is open with live reservations), logs an
    `error` event saying so and re-raises: the order stays open, Close re-runnable. A
    re-close never withdraws (the order is already closed and archived). Dropping the
    Deploy ansible inventory after set_closed is best effort: a failure is an event,
    the close stands."""
    from . import archive, process_report       # archive -> report -> deploy -> stages
    first = ctx.db.closed_at() is None
    # internal, every pass: before anything leaves the order. Not written -> Close stops
    # here: nothing archived, nothing released, the order stays OPEN
    try:
        process_report.write(ctx.dir, ctx.g, ctx.db)
    except Exception as e:
        ctx.db.add_event("error", f"close: process report not written ({type(e).__name__}: "
                                  f"{e}); nothing archived or released, order stays open")
        raise
    at = ctx.db.closed_at() or archive.now()    # a re-close keeps the first close time
    p = archive.payload(ctx.g, ctx.db, ctx.dir, closed_at=at)
    counts = ctx.kea.archive(p)                 # raises KeaError -> order stays open
    try:
        n = ctx.kea.delete_source(source(ctx))
    except Exception as e:
        undo = _retract(ctx, p) if first else "archive kept (order already closed)"
        ctx.db.add_event("error", f"close: reservations NOT released ({e}); {undo}; "
                                  "order stays open")
        raise
    ctx.db.set_closed(at)
    try:
        (Path(ctx.dir) / INVENTORY).unlink(missing_ok=True)
    except OSError as e:
        ctx.db.add_event("error", f"close: Deploy inventory {INVENTORY} not removed: {e}")
    ndev = (counts or {}).get("devices", len(p["devices"]))
    ctx.db.add_event("close", f"closed; archived {ndev} device(s); "
                              f"{n} reservation(s) released")
    return {"released": n, "archived": counts or {}}
