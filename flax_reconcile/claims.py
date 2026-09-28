"""BMC-FW claim sentinel reader.

The triage bmc_fw worker holds /run/flax/bmc-fw-active/<port> while it owns a
slot's power for a firmware flash. flax-reconcile must not flap/kick/power a
claimed port (mirrors the intentional-flap / sol-active guards).

The sentinel filename uses the INTERNAL short port form (e.g. ``et6b1``), NOT
the Arista canonical form (``Ethernet6/1``):
  1. The Arista form contains a ``/`` which would make ``Ethernet6/1`` a
     subdirectory under the claim dir rather than a flat sentinel file.
  2. The triage ``bmc_fw`` worker WRITES the sentinel from the port tokens it
     reads off flax-control ``/api/v1/ports`` -- which are ``observe_state``
     keys in internal ``et6b1`` form.
Reconcile requests carry MIXED port forms (Arista for the auto path, internal
for some operator requests), so cycle.py converts via portname.to_internal
before checking the claim.

Two more /run/flax inputs, same internal port keying (written by the fw bins
bmc-blade-power-cycle / bmc-fw-update / bios-fw-update):
  * /run/flax/bmc-fw-manual/<port> -- a bin-owned claim. The bin's parent
    touches it every 60 s while alive, so it is a HEARTBEAT: active only while
    younger than manual_claim_max_age_secs (a bin killed -9 leaves the file
    behind; it must stop blocking reconcile once it goes stale).
  * /run/flax/bmc-reboot/<port> -- a reboot marker; its mtime is the moment a
    bin rebooted that BMC. Within boot_grace_secs the BMC is still booting.

cycle.py passes the *_DIR constants explicitly at call time so tests can
repoint them (no test touches /run/flax).
"""
import os

BMC_FW_ACTIVE_DIR = "/run/flax/bmc-fw-active"
BMC_FW_MANUAL_DIR = "/run/flax/bmc-fw-manual"
BMC_REBOOT_DIR = "/run/flax/bmc-reboot"


def bmc_fw_claim_active(port, claim_dir=BMC_FW_ACTIVE_DIR):
    """True iff a claim sentinel exists for `port` (internal et6b1 form)."""
    if not port:
        return False
    return os.path.exists(os.path.join(claim_dir, port))


def _younger_than(path, now, max_age_secs):
    try:
        return now - os.path.getmtime(path) < max_age_secs
    except OSError:
        return False


def bmc_manual_claim_active(port, now, max_age_secs=180,
                            claim_dir=BMC_FW_MANUAL_DIR):
    """True iff a fw bin's heartbeat claim for `port` (internal et6b1 form)
    exists and was touched less than max_age_secs ago."""
    if not port:
        return False
    return _younger_than(os.path.join(claim_dir, port), now, max_age_secs)


def bmc_reboot_recent(port, now, grace_secs, reboot_dir=BMC_REBOOT_DIR):
    """True iff a fw bin marked a BMC reboot on `port` (internal et6b1 form)
    less than grace_secs ago. The BMC is booting: a switch_flap would only
    flush the FDB and restart the reservation loop (2026-09-27 report)."""
    if not port:
        return False
    return _younger_than(os.path.join(reboot_dir, port), now, grace_secs)
