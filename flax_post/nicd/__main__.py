"""Entrypoint: wire real deps, run the probe/enforce loop. `python -m flax_post.nicd`.
No control API (NIC has no manual-flash endpoint) — a plain daemon loop."""
import logging
import time

log = logging.getLogger("flax-post-nicd")

from .. import queries, state
from ..fwd import creds as fwd_creds
from ..fwd.redfish import RedfishClient
from . import bridge, config, creds, driver, manifest, store
from .service import Registry, enforce_once, probe_once


class _Deps:
    def __init__(self):
        self.host_creds = creds.load_host_creds()
        self.redfish_creds = fwd_creds.load_redfish_creds()  # [{bmcuser,bmcpass}] for Manager.Reset
        self.matcher = manifest.NicMatcher(manifest.load_nic_manifest(config.CONFIG_DIR))
        self._install_set_row()

    def _install_set_row(self):
        def set_row(port, **fields):
            row = store.set_row(port, **fields)
            try:
                bridge.mirror_row(state.set_state, port, row)
            except Exception:
                log.exception("post_state mirror failed for %s", port)
            return row
        self.set_row = set_row

    def _devices(self):
        return queries.post_devices()

    def hosts(self):
        out = []
        for d in self._devices():
            if d.get("kind") != "host":
                continue
            ip = d.get("lease_ip") or d.get("reservation_ip")
            if ip:
                out.append({**d, "host_ip": ip})
        return out

    def bmc_ip(self, port):
        for d in self._devices():
            if d.get("kind") == "bmc" and d.get("port") == port:
                return d.get("lease_ip") or d.get("reservation_ip")
        return None

    def run(self, ip, script):
        u, p = self.host_creds
        return driver.run_over_ssh(u, p, ip, script)

    def redfish_bmc_reset(self, port):
        # Reboot the BMC via Redfish Manager.Reset (out of context for SSH/IPMI).
        bip = self.bmc_ip(port)
        if not bip:
            return False, "no bmc ip for %s" % port
        return RedfishClient(bip, self.redfish_creds).manager_reset()

    def _vars(self, port):
        return state.read_state().get(port) or {}

    def bmc_phase(self, port):
        return (self._vars(port).get("fw_bmc") or {}).get("phase")

    def bios_phase(self, port):
        return (self._vars(port).get("fw_bios") or {}).get("phase")

    def last_row(self, port):
        return store.read().get(port) or {}


def _scan_loop(deps, registry):
    while True:
        try:
            probe_once(deps, registry)
        except Exception:
            log.exception("probe pass failed")
        try:
            if config.MODE == "enforce":
                enforce_once(deps, registry, config.MODE, config.ENABLE_PORTS)
        except Exception:
            log.exception("enforce pass failed")
        time.sleep(config.PROBE_INTERVAL_S)


def main():
    logging.basicConfig(level=logging.INFO)
    log.info("flax-post-nicd starting; mode=%s allow=%s max_parallel=%d",
             config.MODE, config.ENABLE_PORTS or "(all)", config.MAX_PARALLEL)
    deps = _Deps()
    _scan_loop(deps, Registry())


if __name__ == "__main__":
    main()
