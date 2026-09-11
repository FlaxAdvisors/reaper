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
from . import gc, ipmi

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


def _power_loop():
    while True:
        run_power_pass()
        time.sleep(POWER_INTERVAL_S)


LADDER_PORTS = [p.strip() for p in os.environ.get("FLAX_POST_LADDER_PORTS", "").split(",") if p.strip()]


def slot_ports() -> list:
    """Every geometry slot port, the set the slot workers cover."""
    from .. import geometry
    return [s["port"] for s in geometry.load_geometry()["slots"] if s.get("port")]


def main():
    logging.basicConfig(level=logging.INFO)
    log.info("flax-post producers starting; full=%ss power=%ss", PROBE_INTERVAL_S, POWER_INTERVAL_S)
    threading.Thread(target=_power_loop, name="power-lane", daemon=True).start()
    from ..app import _blade_slots
    from . import worker
    feed = worker.SlotFeed(_blade_slots).start()
    worker.start_workers(slot_ports(), worker.RealDeps(feed), LADDER_PORTS)
    while True:
        run_pass()
        run_gc_pass()
        time.sleep(PROBE_INTERVAL_S)


if __name__ == "__main__":
    main()
