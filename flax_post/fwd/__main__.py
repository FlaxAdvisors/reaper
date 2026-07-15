"""Entrypoint: wire real deps, start the probe loop, serve the control API.

Run as `python -m flax_post.fwd`. Binds the control API to loopback (only the
same-host viewer proxies to it). The probe loop runs in a daemon thread.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("flax-post-fwd")

import uvicorn

from .. import queries, records, state
from . import artifact, bridge, config, creds, manifest, store
from .redfish import RedfishClient
from .service import FlashRegistry, build_app, enforce_once, probe_once


class _Deps:
    def __init__(self):
        self._creds = creds.load_redfish_creds()
        self.matcher = manifest.PostMatcher(manifest.load_manifest(config.CONFIG_DIR))
        self.share_base = config.SHARE_BASE
        self.fetch = artifact.fetch
        self._install_set_row()

    def _install_set_row(self):
        """set_row writes the file store AND mirrors the row into post_state.vars.fw_bmc
        so the main rack view renders the Firmware phase. The mirror is best-effort:
        a DB blip must never break a probe/flash write."""
        def set_row(port, **fields):
            row = store.set_row(port, **fields)
            try:
                bridge.mirror_row(state.set_state, port, row)
            except Exception:
                log.exception("post_state mirror failed for %s", port)
            return row
        self.set_row = set_row

    def post_bmcs(self):
        return [d for d in queries.post_devices() if d.get("kind") == "bmc"]

    def client_for(self, bmc_ip):
        return RedfishClient(bmc_ip, self._creds)

    def record_flash(self, port, bmc_mac, terminal, timeline):
        """Best-effort phase-4 fw-flash work record at a gauntlet's terminal
        transition. Identity comes from the durable post_node row (observe
        writes host_mac + serial there); no resolvable identity -> skip."""
        try:
            p0, serial = records.node_identity(bmc_mac)
            if p0 is None:
                return
            records.record_flash(
                p0_mac=p0, serial=serial, port=port, terminal=terminal,
                row=store.read().get(port) or {}, timeline=timeline,
                keys=records.role_keys(state.read_settings()))
        except Exception:
            log.exception("work-record fw-flash write failed for %s", port)

    def record_action(self, port, bmc_mac, action, ok, detail):
        try:
            p0, serial = records.node_identity(bmc_mac)
            if p0 is None:
                return
            records.record_action(
                p0_mac=p0, serial=serial, port=port, action=action, ok=ok,
                detail=detail, keys=records.role_keys(state.read_settings()))
        except Exception:
            log.exception("work-record fw-action write failed for %s", port)


def _scan_loop(deps, registry, executor):
    while True:
        try:
            probe_once(deps, registry)                     # always: detect/report into fw_bmc
        except Exception:
            log.exception("probe pass failed")
        try:
            enforce_once(deps, registry, executor,         # act only in enforce mode
                         mode=config.MODE, allowlist=config.ENABLE_PORTS)
        except Exception:
            log.exception("enforce pass failed")
        time.sleep(config.PROBE_INTERVAL_S)


def main():
    logging.basicConfig(level=logging.INFO)
    log.info("flax-post-fwd starting; mode=%s allow=%s max_parallel=%d",
             config.MODE, config.ENABLE_PORTS or "(all)", config.MAX_PARALLEL)
    deps = _Deps()
    registry = FlashRegistry()
    executor = ThreadPoolExecutor(max_workers=config.MAX_PARALLEL)
    threading.Thread(target=_scan_loop, args=(deps, registry, executor), daemon=True).start()
    try:
        uvicorn.run(build_app(deps, registry=registry),
                    host=config.CONTROL_HOST, port=config.CONTROL_PORT)
    finally:
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
