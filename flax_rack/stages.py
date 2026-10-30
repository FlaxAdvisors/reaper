"""Stage orchestration for a rack order: the shared context and rung recorder; Discover
itself is discover_ladder (Post's 16-step ladder per device).

Close deletes the order's Kea rows through flax-control FIRST; the order is marked
closed only after that succeeds, so a failure leaves it open and Close re-runnable.
Close needs Kea only: `creds` may be a zero-arg loader, called the first time a
Discover rung needs the switch or BMC login, so missing creds never block Close.
"""
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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


def close_order(ctx) -> int:
    n = ctx.kea.delete_source(source(ctx))      # raises KeaError -> order stays open
    ctx.db.set_closed()
    ctx.db.add_event("close", f"closed; {n} reservation(s) released")
    return n
