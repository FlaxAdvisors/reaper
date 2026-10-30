"""python -m flax_rack: rack-order CLI (import-bom, enable/disable, serve, discover, firmware,
qualify, close).

Order-dir commands act on the current directory (one dir per rack).
"""
import argparse
import json
import os
import sys
from pathlib import Path

from .geometry import load_geometry
from .orderdb import OrderDB


def _import_bom(a) -> int:
    from .bom_import import import_bom
    out = Path.cwd() / "geometry.json"
    if out.exists() and not a.force:
        print(f"{out} exists; pass --force to overwrite", file=sys.stderr)
        return 2
    geo, contradictions = import_bom(a.xlsx, order=a.order, customer=a.customer,
                                     site=a.site, vid=a.vid, prefix=a.prefix)
    out.write_text(json.dumps(geo, indent=2) + "\n")
    print(f"wrote {out}: {len(geo['devices'])} devices, {len(geo['switches'])} switches")
    for c in contradictions:
        print(f"contradiction: {c}")
    print("seed only: set rack tag, mgmt_ip, uplink_ports, iperf_server and any replaced"
          " switch model (the BOM names the original) by hand")
    return 0


def _enable(a) -> int:
    p = Path.cwd() / ".rackpost"
    if a.cmd == "enable":
        if not (Path.cwd() / "geometry.json").is_file():
            print("no geometry.json here", file=sys.stderr)
            return 2
        p.touch()
    else:
        p.unlink(missing_ok=True)
    print(f"{a.cmd}d {Path.cwd()}")
    return 0


def _serve(a) -> int:
    from .server import make_server
    if os.environ.get("RACKPOST_FAKE") == "1":
        from .fakes import fake_context_factory as factory
        print("RACKPOST_FAKE=1: fake switch/Kea/BMC (UI check only)")
    else:
        from .server import default_context_factory as factory
    srv = make_server(Path(a.root), port=a.port, bind=a.bind, context_factory=factory)
    srv.app.start_pollers()
    print(f"flax-rackpost: serving {a.root} on {a.bind}:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.app.stop_pollers()
        srv.server_close()
    return 0


def _order_ctx(confirm=None):
    """(context, held stage lock) for the order dir in cwd; exits on a refusal.
    Creds are not read here (they load on Discover's first switch/BMC rung), so
    Close works with Kea alone."""
    from .server import RackLock, default_context_factory
    d = Path.cwd()
    g = load_geometry(d / "geometry.json")
    if confirm is not None and confirm != g.order:
        sys.exit(f"--confirm must be the order id {g.order}")
    db = OrderDB(d / "order.db")
    db.init(g)
    lock = RackLock(d)
    if not lock.acquire():
        sys.exit("a stage is already running for this rack")
    try:
        return default_context_factory(d, g, db), lock
    except BaseException:
        lock.release()
        raise


def _discover(a) -> int:
    from .creds import CredsError
    from .stages import run_discover
    if not a.pos and a.confirm is None:
        sys.exit("a rack-wide discover may power-cycle nodes: --confirm <order id> "
                 "(or --pos for some positions)")
    ctx, lock = _order_ctx(None if a.pos else a.confirm)
    try:
        run_discover(ctx, positions=a.pos or None)
    except (CredsError, ValueError) as e:
        sys.exit(str(e))
    finally:
        lock.release()
    for (pos, rung), r in sorted(ctx.db.latest_runs().items()):
        print(f"{pos:8s} {rung:12s} {r['verdict'] or r['status']}")
    return 0


def _firmware(a) -> int:
    from flax_rungs.fwsteps import FW_STEPS
    from .creds import CredsError
    from .firmware import run_firmware
    ctx, lock = _order_ctx()
    try:
        run_firmware(ctx, positions=a.pos or None)
    except (CredsError, ValueError) as e:
        sys.exit(str(e))
    finally:
        lock.release()
    lr = ctx.db.latest_runs()
    keys = sorted({k for k, rung in lr if rung in FW_STEPS})
    for key in keys:
        for rung in FW_STEPS:
            r = lr.get((key, rung))
            if r:
                det = r["detail"] or {}
                print(f"{key:12s} {rung:13s} {r['verdict'] or r['status']:5s} "
                      f"{det.get('state') or '':8s} {det.get('headline') or ''}")
    return 0


def _qualify(a) -> int:
    from .creds import CredsError
    from .qualify import device_rungs, qualify_passed, run_qualify
    if not a.pos and a.confirm is None:
        sys.exit("a rack-wide qualify power-cycles every host: --confirm <order id> "
                 "(or --pos for some positions)")
    ctx, lock = _order_ctx(None if a.pos else a.confirm)
    try:
        run_qualify(ctx, positions=a.pos or None)
    except (CredsError, ValueError) as e:
        sys.exit(str(e))
    finally:
        lock.release()
    lr = ctx.db.latest_runs()
    for d in ctx.g.devices:
        if a.pos and d.pos not in a.pos:
            continue
        for rung in device_rungs(d) + ["fw-versions"]:
            r = lr.get((d.pos, rung))
            if r:
                det = r["detail"] or {}
                why = det.get("error") or det.get("reason") or ""
                print(f"{d.pos:8s} {rung:14s} {r['verdict'] or r['status']}"
                      + (f"  {why}" if why else ""))
    ok, bad = qualify_passed(ctx.db, ctx.g)
    print("qualify: rack passed" if ok else f"qualify: not passed: {', '.join(bad)}")
    return 0 if ok else 1


def _close(a) -> int:
    from .kea_client import KeaError
    from .stages import close_order
    ctx, lock = _order_ctx(a.confirm)
    try:
        n = close_order(ctx)
    except KeaError as e:
        print(f"close failed, order stays open: {e}", file=sys.stderr)
        return 1
    finally:
        lock.release()
    print(f"closed {ctx.g.order}; {n} reservation(s) released")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m flax_rack")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-bom", help="seed ./geometry.json from a BOM .xlsx")
    p.add_argument("xlsx")
    p.add_argument("--order", required=True)
    p.add_argument("--customer", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--vid", type=int, required=True)
    p.add_argument("--prefix", required=True, help="hostname prefix, e.g. rr101-")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=_import_bom)
    for name in ("enable", "disable"):
        sub.add_parser(name, help=f"{name} serving of this order dir").set_defaults(fn=_enable)
    p = sub.add_parser("serve", help="run the HTTP daemon")
    p.add_argument("--root", default="/export/nodes")
    p.add_argument("--port", type=int, default=8452)
    p.add_argument("--bind", default="127.0.0.1")
    p.set_defaults(fn=_serve)
    p = sub.add_parser("discover", help="run Discover for this order dir (powers on / cycles "
                                         "nodes not in the live image)")
    p.add_argument("--pos", action="extend", nargs="+", metavar="P",
                   help="only these positions / switch ids (--pos L37 A)")
    p.add_argument("--confirm", metavar="ORDER", help="the order id (required without --pos)")
    p.set_defaults(fn=_discover)
    p = sub.add_parser("firmware", help="read firmware versions (detect only; never flashes)")
    p.add_argument("--pos", action="extend", nargs="+", metavar="P",
                   help="only these positions / switch ids (--pos L37 A)")
    p.set_defaults(fn=_firmware)
    p = sub.add_parser("qualify", help="run Qualify for this order dir (or some positions)")
    p.add_argument("--pos", action="extend", nargs="+", metavar="P",
                   help="only these positions (--pos L37 C37 or repeated)")
    p.add_argument("--confirm", metavar="ORDER", help="the order id (required without --pos)")
    p.set_defaults(fn=_qualify)
    p = sub.add_parser("close", help="release this rack's reservations and close it")
    p.add_argument("--confirm", required=True, metavar="ORDER")
    p.set_defaults(fn=_close)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
