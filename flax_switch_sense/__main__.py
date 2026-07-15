"""Daemon entrypoint: python -m flax_switch_sense

Boots one SwitchFetcher per switch, exposes /healthz, handles SIGTERM.

CLI flags:
  --check-vip-holder   Exit 0 iff this bang holds MGMT_VIP per /etc/flax/site.env.
                       systemd uses this as ExecStartPre so the unit only runs
                       on the VIP master.
"""
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from .fetcher import SwitchFetcher, load_switches, make_driver
from .healthz import FetcherStatus, build_handler
from .macmath import load_macmath_dir


log = logging.getLogger("flax-switch-sense")


def _read_site_env(path: str = "/etc/flax/site.env") -> dict:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _ip_held_locally(ip: str) -> bool:
    """True iff `ip` is assigned to any local interface."""
    if not ip:
        return False
    try:
        import ipaddress
        ipaddress.ip_address(ip)
    except ValueError:
        return False
    import subprocess
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr"], timeout=2).decode()
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return any(line.split()[3].split("/")[0] == ip for line in out.splitlines() if len(line.split()) >= 4)


def check_vip_holder() -> int:
    env = _read_site_env()
    vip = env.get("MGMT_VIP", "")
    if not vip:
        log.error("MGMT_VIP not set in /etc/flax/site.env -- refusing to start")
        return 1
    return 0 if _ip_held_locally(vip) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flax-switch-sense")
    parser.add_argument("--check-vip-holder", action="store_true",
                        help="exit 0 iff MGMT_VIP is on this host")
    parser.add_argument("--switches", default="/etc/flax/switches.json",
                        help="path to switches.json")
    parser.add_argument("--credentials", default="/etc/flax/credentials.json",
                        help="path to credentials.json")
    parser.add_argument("--macmath-dir", default="/etc/flax/macmath",
                        help="dir of per-vid macmath configs (<vid>.json); "
                             "absent dir -> legacy ±2 pairing for every port")
    parser.add_argument("--cycle-secs", type=float, default=10.0)
    parser.add_argument("--healthz-port", type=int, default=10989)
    parser.add_argument("--healthz-stale-secs", type=float, default=30.0,
                        help="/healthz returns 503 if any fetcher hasn't polled in this many seconds")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.check_vip_holder:
        return check_vip_holder()

    with open(args.credentials) as f:
        credentials = json.load(f)
    switches_config = load_switches(args.switches)

    # Load per-vid macmath configs ONCE at startup; threaded into every
    # fetcher so each port classifies by its access_vid. Absent dir -> {}.
    macmath_by_vid = load_macmath_dir(args.macmath_dir)
    log.info("loaded macmath configs for %d vid(s) from %s",
             len(macmath_by_vid), args.macmath_dir)

    # Build per-switch fetcher threads + status objects keyed by switch name.
    statuses: dict[str, FetcherStatus] = {}
    fetchers: list[SwitchFetcher] = []
    for entry in switches_config:
        name = entry["name"]
        driver = make_driver(entry, credentials)
        fetcher = SwitchFetcher(name, driver, cycle_secs=args.cycle_secs,
                                macmath_by_vid=macmath_by_vid)
        statuses[name] = FetcherStatus(switch=name, last_polled=None, last_error=None)

        # Wire fetcher status updates into the statuses dict.
        original_poll = fetcher._poll_once

        def make_wrapped_poll(orig, status_ref):
            def wrapped_poll(_orig=orig, _status_ref=status_ref):
                _orig()
                _status_ref.last_polled = time.time()
                _status_ref.last_error = None
            return wrapped_poll

        fetcher._poll_once = make_wrapped_poll(original_poll, statuses[name])
        fetchers.append(fetcher)

    # HTTP /healthz
    handler_cls = build_handler(statuses, max_stale_secs=args.healthz_stale_secs)
    server = ThreadingHTTPServer(("0.0.0.0", args.healthz_port), handler_cls)
    http_thread = threading.Thread(target=server.serve_forever,
                                   name="healthz", daemon=True)
    http_thread.start()

    # Start fetchers
    for f in fetchers:
        f.start()

    # Signal handling
    stop_event = threading.Event()
    def on_signal(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        stop_event.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)

    log.info("flax-switch-sense up; %d fetcher(s), /healthz on :%d",
             len(fetchers), args.healthz_port)
    stop_event.wait()

    for f in fetchers:
        f.stop()
    server.shutdown()
    for f in fetchers:
        f.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
