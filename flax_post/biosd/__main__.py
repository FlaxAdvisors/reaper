"""Entrypoint: wire real deps, run the probe loop.

Run as `python -m flax_post.biosd`. Unlike flax_post.fwd there is no control
API — BIOS has no manual-flash endpoint — so this is a plain daemon loop in
the main thread (no uvicorn).
"""
import logging
import time

log = logging.getLogger("flax-post-biosd")

from .. import queries, state
from . import bridge, config, creds, driver, manifest, store
from .service import Registry, enforce_once, probe_once


class _Deps:
    def __init__(self):
        self.creds = creds.load_host_creds()
        self.matcher = manifest.BiosMatcher(manifest.load_bios_manifest(config.CONFIG_DIR))
        self.share_base = config.SHARE_BASE
        self._install_set_row()

    def _install_set_row(self):
        """set_row writes the file store AND mirrors the row into
        post_state.vars.fw_bios so the main rack view renders the BIOS phase.
        The mirror is best-effort: a DB blip must never break a probe write."""
        def set_row(port, **fields):
            row = store.set_row(port, **fields)
            try:
                bridge.mirror_row(state.set_state, port, row)
            except Exception:
                log.exception("post_state mirror failed for %s", port)
            return row
        self.set_row = set_row

    def hosts(self):
        """Post 'host' devices with a reachable IP (lease_ip if leased, else
        the DHCP reservation) — the booted node we SSH into for afulnx."""
        out = []
        for d in queries.post_devices():
            if d.get("kind") != "host":
                continue
            ip = d.get("lease_ip") or d.get("reservation_ip")
            if not ip:
                continue
            out.append({**d, "host_ip": ip})
        return out

    def run(self, ip, script):
        user, pw = self.creds
        return driver.run_over_ssh(user, pw, ip, script)

    def bmc_phase(self, port):
        row = state.read_state().get(port) or {}
        return (row.get("fw_bmc") or {}).get("phase")

    def last_row(self, port):
        return store.read().get(port) or {}


def _scan_loop(deps, registry):
    while True:
        try:
            probe_once(deps, registry)                      # always: detect/report into fw_bios
        except Exception:
            log.exception("probe pass failed")
        try:
            if config.MODE == "enforce":                    # act only in enforce mode
                enforce_once(deps, registry, config.MODE, config.ENABLE_PORTS)
        except Exception:
            log.exception("enforce pass failed")
        time.sleep(config.PROBE_INTERVAL_S)


def main():
    logging.basicConfig(level=logging.INFO)
    log.info("flax-post-biosd starting; mode=%s allow=%s max_parallel=%d",
             config.MODE, config.ENABLE_PORTS or "(all)", config.MAX_PARALLEL)
    deps = _Deps()
    registry = Registry()
    _scan_loop(deps, registry)


if __name__ == "__main__":
    main()
