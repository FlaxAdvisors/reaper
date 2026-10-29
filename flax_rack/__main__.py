"""python -m flax_rack: rack-order CLI (import-bom, enable/disable, serve, discover, close).

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
    print("seed only: set rack tag, mgmt_ip, uplink_ports and iperf_server by hand")
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
    print(f"flax-rackpost: serving {a.root} on {a.bind}:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def _order_ctx(confirm=None):
    """(context, held stage lock) for the order dir in cwd; exits on a refusal."""
    from .creds import CredsError
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
    except CredsError as e:
        lock.release()
        sys.exit(str(e))


def _discover(a) -> int:
    from .stages import run_discover
    ctx, lock = _order_ctx()
    try:
        run_discover(ctx)
    finally:
        lock.release()
    for (pos, rung), r in sorted(ctx.db.latest_runs().items()):
        print(f"{pos:8s} {rung:12s} {r['verdict'] or r['status']}")
    return 0


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
    p.add_argument("--port", type=int, default=8450)
    p.add_argument("--bind", default="127.0.0.1")
    p.set_defaults(fn=_serve)
    sub.add_parser("discover", help="run Discover for this order dir").set_defaults(fn=_discover)
    p = sub.add_parser("close", help="release this rack's reservations and close it")
    p.add_argument("--confirm", required=True, metavar="ORDER")
    p.set_defaults(fn=_close)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
