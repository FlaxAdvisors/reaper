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
"""
import os

BMC_FW_ACTIVE_DIR = "/run/flax/bmc-fw-active"


def bmc_fw_claim_active(port, claim_dir=BMC_FW_ACTIVE_DIR):
    """True iff a claim sentinel exists for `port` (internal et6b1 form)."""
    if not port:
        return False
    return os.path.exists(os.path.join(claim_dir, port))
