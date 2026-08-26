"""flax-post producers entrypoint: `python -m flax_post.observe`.

Runs the owned IPMI producer in a loop, guarded so a pass failure never kills
the loop (mirrors flax_post/fwd/__main__._probe_loop). Sole writer of
post_state/post_node; the viewer process stays read-mostly.
"""
import logging
import os
import threading
import time

from .. import records
from . import gc, ipmi, host_qual

log = logging.getLogger("flax-post.observe")
PROBE_INTERVAL_S = int(os.environ.get("FLAX_POST_OBSERVE_INTERVAL", "15"))
# Power + bmc-liveness run on their own faster lane so a power change reflects on
# the rack tile in seconds, not behind the heavy serial/SDR/SEL pass.
POWER_INTERVAL_S = int(os.environ.get("FLAX_POST_POWER_INTERVAL", "6"))


def run_pass(ipmi_once=None) -> None:
    try:
        if ipmi_once is not None:
            ipmi_once()
        else:
            ipmi.run_once(record_observation=records.record_observation)
    except Exception:
        log.exception("ipmi producer pass failed")


def run_power_pass(power_once=ipmi.run_power_once) -> None:
    try:
        power_once()
    except Exception:
        log.exception("ipmi power pass failed")


def run_gc_pass(gc_once=None) -> None:
    # Default resolved at call time (not bind time) via module attribute lookup
    # so tests can `mock.patch.object(__main__, "gc")` and still observe the call.
    try:
        summary = (gc_once or gc.gc_post_state)()
        if summary.get("deleted"):
            log.info("post_state gc deleted=%d latched=%d",
                     summary["deleted"], summary.get("latched", 0))
    except Exception:
        log.exception("post_state gc pass failed")


def qual_targets(slots) -> list:
    """Booted post blades (host_ip present) whose on-node agent may be up."""
    out = []
    for s in slots:
        if s.get("empty") or not s.get("host_ip"):
            continue
        out.append({"port": s["port"], "host_ip": s["host_ip"], "bmc_ip": s.get("bmc_ip"),
                    "bmc_mac": s.get("bmc_mac"), "serial": s.get("serial"),
                    "order_no": s.get("order_no"),
                    # phase == "Qualify" means Discover+Firmware are done but Qualify
                    # isn't -> host_qual launches the agent (no reboot). See poll_target.
                    "phase": s.get("phase")})
    return out


def _live_targets() -> list:
    """Build qual targets from the current blade view (imported lazily to avoid a
    viewer<->producer import cycle)."""
    from ..app import _blade_slots
    return qual_targets(_blade_slots())


def run_host_qual_pass(targets_fn=_live_targets, once=host_qual.run_once) -> None:
    try:
        once(targets_fn())
    except Exception:
        log.exception("host_qual pass failed")


def _power_loop():
    while True:
        run_power_pass()
        time.sleep(POWER_INTERVAL_S)


def main():
    logging.basicConfig(level=logging.INFO)
    log.info("flax-post producers starting; full=%ss power=%ss", PROBE_INTERVAL_S, POWER_INTERVAL_S)
    threading.Thread(target=_power_loop, name="power-lane", daemon=True).start()
    while True:
        run_pass()
        run_gc_pass()
        run_host_qual_pass()
        time.sleep(PROBE_INTERVAL_S)


if __name__ == "__main__":
    main()
