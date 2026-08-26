"""Probe loop + FastAPI control surface for the post firmware driver.

build_app(deps) returns a FastAPI app exposing GET /healthz.
deps is injected (real wiring in __main__, fakes in tests) and supplies:
  post_bmcs() -> [device dict]   (kind == 'bmc' rows from queries.post_devices)
  client_for(bmc_ip) -> RedfishClient
  matcher, fetch(share_base, rel)->bytes, set_row(port, **f), share_base
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import enforce, flasher

# Each BMC probe is an independent Redfish session; fan out so a 48-blade pass is
# bounded by one slow BMC, not their sum. Set 1 to force the sequential path (tests).
DEFAULT_PROBE_WORKERS = int(os.environ.get("FLAX_POST_FWD_WORKERS", "48"))


class FlashRegistry:
    """Thread-safe set of ports with an in-flight flash.

    Shared between enforce_once (which claims a port for the duration of an
    autonomous flash) and the probe loop (which skips any claimed port so a
    routine probe pass cannot clobber the flash's phase writes).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = set()

    def claim(self, port) -> bool:
        """Mark port in-flight; return False if it was already claimed."""
        with self._lock:
            if port in self._active:
                return False
            self._active.add(port)
            return True

    def release(self, port) -> None:
        with self._lock:
            self._active.discard(port)

    def is_flashing(self, port) -> bool:
        with self._lock:
            return port in self._active


def probe_once(deps, registry=None, workers=None) -> None:
    """One probe pass over every post BMC: classify current-vs-target into the store.

    Fanned out across a worker pool (each BMC is an independent Redfish session).
    Skips any port the registry reports as mid-flash — the flash's own state
    writes (checking/monitoring/done) are authoritative while it runs. `workers`
    defaults to FLAX_POST_FWD_WORKERS (48); set 1 to force the sequential path.
    """
    bmcs = [d for d in deps.post_bmcs()
            if not (registry is not None and registry.is_flashing(d["port"]))]
    if not bmcs:
        return

    def work(dev):
        client = deps.client_for(dev["reservation_ip"])
        set_row = lambda port, _ip=dev["reservation_ip"], **f: deps.set_row(port, bmc_ip=_ip, **f)
        flasher.probe_one(dev["port"], client, deps.matcher, set_row)

    n = DEFAULT_PROBE_WORKERS if workers is None else workers
    n = max(1, min(n, len(bmcs)))
    if n == 1:
        for dev in bmcs:
            work(dev)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(work, bmcs))


def enforce_once(deps, registry, executor, *, mode, allowlist, run_node=enforce.run_node):
    """Submit each allowlisted, not-in-flight candidate BMC's gauntlet to the executor.
    Fire-and-forget (no barrier): per-node threads run independently. No-op in detect mode.

    Phase-4 record hooks (both optional — getattr-discovered so bare deps work):
      deps.record_flash(port, bmc_mac, terminal, timeline)  — one fw-flash record
        per gauntlet run, at its terminal transition; timeline is the ordered
        [{"phase", "at"}] list of every phase the run's set_row wrote.
      deps.record_action(port, bmc_mac, action, ok, detail) — per owned action.
    """
    if mode != "enforce":
        return
    record_flash = getattr(deps, "record_flash", None)
    record_action = getattr(deps, "record_action", None)
    for dev in deps.post_bmcs():
        port = dev["port"]
        if not enforce.gate_allows(port, mode, allowlist):
            continue
        if not registry.claim(port):
            continue                        # already in flight (this scan or a prior one)

        def _run(dev=dev, port=port):
            timeline = []

            def set_row(p, _ip=dev["reservation_ip"], **f):
                if "phase" in f:
                    timeline.append({"phase": f["phase"], "at": time.time()})
                return deps.set_row(p, bmc_ip=_ip, **f)

            kw = {}
            if record_action is not None:
                kw["on_action"] = (lambda a, ok, det, _p=port, _m=dev.get("mac"):
                                   record_action(_p, _m, a, ok, det))
            try:
                client = deps.client_for(dev["reservation_ip"])
                terminal = run_node(port, client, deps.matcher, deps.fetch,
                                    set_row, deps.share_base, **kw)
                if record_flash is not None:
                    record_flash(port, dev.get("mac"), terminal, timeline)
            except Exception:  # a dead thread must not strand the port claimed forever
                try:
                    deps.set_row(port, bmc_ip=dev["reservation_ip"], phase="fault")
                except Exception:
                    pass
            finally:
                registry.release(port)

        executor.submit(_run)


def build_app(deps, *, registry=None) -> FastAPI:
    # `registry` is accepted for __main__ call-site compatibility but is no longer
    # used here — the FlashRegistry is owned by the scan loop (probe_once/enforce_once),
    # not the app, since the manual /flash endpoint was removed.
    app = FastAPI(title="flax-post-fwd")

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"status": "ok"})

    return app
