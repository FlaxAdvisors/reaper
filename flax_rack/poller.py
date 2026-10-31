"""BMC poller: one daemon thread per enabled rack. Every interval it pings each BMC
and reads `chassis power status`, and stores the last look with
OrderDB.set_bmc_live (the tile's power chip). It never creates rung runs and never
acts on a node. Any exception -- one BMC, missing creds, a geometry being edited --
is swallowed until the next tick.
"""
import threading

from flax_rungs.bmc import ping, power_status

from .addressing import plan_addresses
from .creds import CredsError
from .orderdb import _now
from .stages import creds

INTERVAL_S = 60


def poll_once(ctx) -> int:
    """One pass over the rack's BMCs; returns how many were written."""
    try:
        bmc_creds = creds(ctx).bmc
    except CredsError:
        bmc_creds = []                       # ping only; power unknown
    R = getattr(ctx, "rungs", None)          # the served fake's stand-ins, else the real
    pg, ps = getattr(R, "ping", None) or ping, getattr(R, "power_status", None) or power_status
    n = 0
    for a in plan_addresses(ctx.g):
        if a.role != "bmc":
            continue
        try:
            up = pg(a.ip, runner=ctx.runner)
            power = ps(a.ip, bmc_creds, runner=ctx.runner) if up and bmc_creds else None
            ctx.db.set_bmc_live(a.pos, up, power, _now())
            n += 1
        except Exception:                    # noqa: BLE001 -- the next tick tries again
            pass
    return n


class BmcPoller(threading.Thread):
    """ctx_factory() is called every tick, so a re-parsed geometry is picked up."""

    def __init__(self, ctx_factory, interval_s=INTERVAL_S, name="bmc-poller"):
        super().__init__(name=name, daemon=True)
        self.ctx_factory = ctx_factory
        self.interval_s = interval_s
        self._stop_ev = threading.Event()

    def tick(self) -> None:
        try:
            poll_once(self.ctx_factory())
        except Exception:                    # noqa: BLE001
            pass

    def run(self) -> None:
        while not self._stop_ev.is_set():
            self.tick()
            self._stop_ev.wait(self.interval_s)

    def stop(self) -> None:
        self._stop_ev.set()
