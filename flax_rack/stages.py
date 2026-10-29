"""Stage orchestration for a rack order. Discover: FDB -> slot match -> Kea reserve -> BMC reach.

Close deletes the order's Kea rows through flax-control FIRST; the order is marked
closed only after that succeeds, so a failure leaves it open and Close re-runnable.
Close needs Kea only: `creds` may be a zero-arg loader, called the first time a
Discover rung needs the switch or BMC login, so missing creds never block Close.
"""
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from flax_rungs.bmc import ping, reach
from flax_rungs.result import RungResult
from flax_rungs.sonic import FdbEntry, read_fdb

from .addressing import plan_addresses
from typing import Callable

from .creds import Creds
from .discover import match
from .geometry import KIND_ROLES, Geometry
from .kea_client import KeaClient, KeaError
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


def source(ctx) -> str:
    return ctx.g.kea_source


def creds(ctx) -> Creds:
    """The switch/BMC logins, loaded on first use (raises CredsError)."""
    if callable(ctx.creds):
        ctx.creds = ctx.creds()
    return ctx.creds


def _rung(ctx, pos, rung, fn) -> RungResult:
    rid = ctx.db.start_run(pos, STAGE, rung)
    try:
        r = fn()
    except Exception as e:           # a rung never leaves its row 'running'
        r = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
    ctx.db.finish_run(rid, r)
    return r


def run_discover(ctx) -> None:
    if ctx.db.closed_at():
        raise RuntimeError("order is closed")
    g = ctx.g
    sw = next((s for s in g.switches if s.role == "mgmt" and s.mgmt_ip), None)
    if sw is None:
        _rung(ctx, "_rack", "switch-fdb",
              lambda: RungResult("fail", {"error": "no mgmt switch with mgmt_ip in geometry"}))
        return
    user, pw = creds(ctx).sonic
    r = _rung(ctx, "_rack", "switch-fdb",
              lambda: read_fdb(sw.mgmt_ip, user, pw, runner=ctx.runner))
    if r.verdict != "pass":
        return
    fdb = [FdbEntry(**e) for e in r.detail["fdb"]]
    m = match(g, fdb, sw.id)
    addrs: dict[str, list] = {}
    for a in plan_addresses(g):
        addrs.setdefault(a.pos, []).append(a)

    for d in g.devices:
        dm = m.devices[d.pos]
        ok = _rung(ctx, d.pos, "slot-match", lambda: RungResult(
            "pass" if dm.status == "found" else "fail",
            {"status": dm.status, "note": dm.note, "host_mac": dm.host_mac,
             "bmc_mac": dm.bmc_mac, "iom_macs": dm.iom_macs})).verdict == "pass"
        if not ok:
            continue
        ctx.db.set_macs(d.pos, host_mac=dm.host_mac, bmc_mac=dm.bmc_mac, iom_macs=dm.iom_macs)

        def reserve():
            done = []
            for a in addrs.get(d.pos, []):
                if a.role == "host":
                    mac = dm.host_mac
                elif a.role == "bmc":
                    mac = dm.bmc_mac
                else:
                    mac = dm.iom_macs[int(a.role[3:]) - 1]
                try:
                    ctx.kea.upsert(source(ctx), mac, a.ip, a.name, g.vid)
                except KeaError as e:
                    return RungResult("fail", {"error": f"{a.role}: {e}", "reserved": done})
                done.append({"role": a.role, "ip": a.ip, "name": a.name, "mac": mac})
            return RungResult("pass", {"reserved": done})

        if _rung(ctx, d.pos, "reserve", reserve).verdict != "pass":
            continue

        def reachable():
            if KIND_ROLES[d.kind] == "host+bmc":
                ip = next(a.ip for a in addrs[d.pos] if a.role == "bmc")
                return reach(ip, creds(ctx).bmc, runner=ctx.runner)
            # IOMs have no IPMI: ping only
            res = {a.role: ping(a.ip, runner=ctx.runner) for a in addrs[d.pos]}
            return RungResult("pass" if all(res.values()) else "fail", {"ping": res})

        _rung(ctx, d.pos, "bmc-reach", reachable)
    ctx.db.add_event("discover", f"discover done; {len(m.unexpected)} unexpected MAC(s)")


def close_order(ctx) -> int:
    n = ctx.kea.delete_source(source(ctx))      # raises KeaError -> order stays open
    ctx.db.set_closed()
    ctx.db.add_event("close", f"closed; {n} reservation(s) released")
    return n
