"""Free-running per-node BMC firmware gauntlet + staged-rollout gates (spec §4.4).

detect (default) = report-only: the probe loop classifies every BMC, nothing acts.
enforce = for each ALLOWLISTED, not-in-flight blade, run the per-node gauntlet in
its own thread (no cross-node barrier): Discover set-PXE + power-on, then bmc flash.
"""
import time

from . import flasher


def gate_allows(port, mode, allowlist):
    """True only in enforce mode for a port the allowlist admits (empty = whole rack)."""
    if mode != "enforce":
        return False
    return (not allowlist) or (port in allowlist)


def run_node(port, client, matcher, fetch, set_row, share_base,
             *, flash_one=flasher.flash_one, sleep=time.sleep,
             power_wait_s=120, poll_s=20, max_wait_s=1200, on_action=None):
    """Per-node gauntlet (enforce mode). Discover action then bmc-check/update via flash_one.
    No cross-node state — safe to run concurrently for many ports.
    on_action(action, ok, detail), when given, is called for each OWNED action
    (set-pxe, power-on) — the phase-4 fw-action work-record hook."""
    power, detail = client.get_power_state()
    if power is None:
        set_row(port, phase="unreachable", fault_reason="BMC unreachable (power read): %s" % detail)
        return "unreachable"
    if power != "On":
        # Discover owned action: PXE-boot the node so it's up for the flash gate (and,
        # later, BIOS/Qualify). PXE-set is Redfish-only on this wiwynn-tp fleet; a failure
        # is logged via the row, not treated as a flash fault.
        pxe_ok, pxe_detail = client.set_boot_pxe()
        if not pxe_ok:
            set_row(port, fault_reason="set-pxe failed (non-fatal): %s" % pxe_detail)
        if on_action is not None:
            on_action("set-pxe", bool(pxe_ok), "" if pxe_ok else str(pxe_detail))
        p_ok, p_detail = client.power_on()
        if on_action is not None:
            on_action("power-on", bool(p_ok), "" if p_ok else str(p_detail))
        waited = 0
        while power != "On" and waited < power_wait_s:
            sleep(poll_s)
            waited += poll_s if poll_s else 1
            power, _ = client.get_power_state()
        if power != "On":
            set_row(port, phase="unreachable",
                    fault_reason="powered on but not On after %ds; power-cycle to recover" % power_wait_s)
            return "unreachable"
    # host is On -> bmc-check/update (flash_one enforces the power-ON gate + version compare)
    return flash_one(port, client, matcher, fetch, set_row, share_base,
                     sleep=sleep, poll_s=poll_s, max_wait_s=max_wait_s)
