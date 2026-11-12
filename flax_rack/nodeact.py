"""The drawer's PWR and IDNT: operator power and chassis-identify actions on one node's
host BMC (hosts only; IOMs later). The loose-node Post UI's semantics (flax_post.actions:
power on / off / cycle, identify on / off / force), sent with rackpost's own BMC
credentials through flax_rungs.bmc (one credential x cipher session, password in
IPMI_PASSWORD + -E, never argv); `reset` is ipmitool `chassis power reset`.

power: on needs nothing typed; off / cycle / reset need {"confirm": <pos>}. Refused (409)
while a stage or Run holds the rack lock and the node is in its scope (a rack-wide stage,
or a positions run naming it; Qualify / Deploy also cover the iperf server they pull in;
a CLI stage covers the rack). The power read back after the action becomes the tile's
power chip until the BmcPoller's next look.

identify: on = `chassis identify 240` (the LED blinks 240 s), force = until turned off,
off = `chassis identify 0`. Its state = the BMC's "Chassis Identify State" when `chassis
status` reports one, else the last command (+ its expiry), kept in order.db
(OrderDB.set_ident). Never refused by the lock: an LED is no power action.

Every action is an event (kind "power" / "identify", who), which the process report
lists under the pass's operator actions.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from flax_rungs import bmc

from .addressing import plan_addresses
from .creds import CredsError
from .orderdb import _now
from .stages import creds

POWER_ACTIONS = ("on", "off", "cycle", "reset")
IDENT_MODES = ("on", "force", "off")
IDENT_S = 240
_IDENT_ARG = {"on": str(IDENT_S), "force": "force", "off": "0"}


def real_rungs():
    return SimpleNamespace(node_power=bmc.power_action, power_status=bmc.power_status,
                           identify=bmc.identify)


def _rungs(ctx):
    R, real = getattr(ctx, "rungs", None), real_rungs()
    return SimpleNamespace(**{k: getattr(R, k, None) or getattr(real, k) for k in vars(real)})


def host_bmc(g, pos) -> str | None:
    """The node's host BMC IP (a host-bearing device), else None."""
    try:
        d = g.device(pos)
    except KeyError:
        return None
    if not d.endpoints.get("hosts"):
        return None
    return next((a.ip for a in plan_addresses(g) if a.pos == pos and a.role == "bmc"), None)


def _creds(ctx):
    try:
        return creds(ctx).bmc, None
    except CredsError as e:
        return [], str(e)


def power(ctx, pos: str, ip: str, action: str, who: str) -> tuple[int, dict]:
    R = _rungs(ctx)
    cr, err = _creds(ctx)
    info: dict = {}
    ok = bool(cr) and R.node_power(ip, cr, action, info=info, runner=ctx.runner)
    now = None
    if ok:
        try:
            now = R.power_status(ip, cr, runner=ctx.runner)
            ctx.db.set_bmc_live(pos, True, now, _now())
        except Exception:                    # noqa: BLE001 -- the poller reads it next
            now = None
    why = None if ok else err or ("no BMC credentials" if not cr
                                  else "the BMC refused or did not answer")
    ctx.db.add_event("power", f"{pos} power {action} by {who}: "
                              + (f"ok (cipher {info.get('cipher')}; power now {now or '?'})"
                                 if ok else f"FAILED: {why}"))
    if not ok:
        return 502, {"error": f"power {action} {pos}: {why}"}
    return 200, {"pos": pos, "action": action, "power": now, "cipher": info.get("cipher")}


def identify(ctx, pos: str, ip: str, mode: str, who: str) -> tuple[int, dict]:
    R = _rungs(ctx)
    cr, err = _creds(ctx)
    info: dict = {}
    res = R.identify(ip, cr, _IDENT_ARG[mode], info=info, runner=ctx.runner) if cr \
        else {"ok": False, "ident": None, "error": err or "no BMC credentials"}
    if not res.get("ok"):
        ctx.db.add_event("identify", f"{pos} identify {mode} by {who}: FAILED: {res.get('error')}")
        return 502, {"error": f"identify {mode} {pos}: {res.get('error')}"}
    at = datetime.now(timezone.utc)
    until = (at + timedelta(seconds=IDENT_S)).isoformat(timespec="seconds") if mode == "on" else None
    st = {"mode": mode, "at": at.isoformat(timespec="seconds"), "until": until,
          "reported": res.get("ident"), "who": who}
    ctx.db.set_ident(pos, st)
    ctx.db.add_event("identify", f"{pos} identify {mode} by {who}: ok"
                                 + (f" ({IDENT_S} s)" if mode == "on" else "")
                                 + (f"; BMC reports {res['ident']}" if res.get("ident") else ""))
    return 200, {"pos": pos, "ident": ident_view(st)}


def ident_view(st: dict | None, now: datetime | None = None) -> dict | None:
    """The served identify state: {**st, "on"}; on = force, or on and not yet expired (a
    BMC-reported Off read back after an on is believed)."""
    if not st:
        return None
    now = now or datetime.now(timezone.utc)
    rep = (st.get("reported") or "").lower()
    if rep.startswith("off") and st.get("mode") != "off":
        on = False
    elif st.get("mode") == "force":
        on = True
    elif st.get("mode") == "on" and st.get("until"):
        on = datetime.fromisoformat(st["until"]) > now
    else:
        on = False
    return {**st, "on": on}
