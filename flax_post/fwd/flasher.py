# flax_post/fwd/flasher.py
"""Power-on-gated BMC firmware flash state machine for one post node.

Gate (power-on policy): we flash ONLY a powered-ON (idle) host and we never change
power ourselves — the operator powers the node on+idle before triggering. Keeping
the host NIC up lets the BMC re-establish NC-SI cleanly after the activation reboot
(an OFF host is the suspected cause of post-activation BMC hangs). Tioga Pass
success is version-based: after the multipart
POST, TaskState stays 'New' while PercentComplete climbs, the BMC self-activates
(Redfish goes dark ~13-15 min), then returns at the new version — so we poll the
task for progress, then poll get_fw_version until the BMC is back, then verify.

All collaborators (Redfish client, artifact fetch, store writer, sleep) are
injected so the whole machine is unit-testable without a BMC or real sleeps.
"""
import os
import time

from . import manifest


def _fault(set_row, port, reason, **extra):
    set_row(port, phase="fault", fault_reason=reason, **extra)
    return "fault"


def _unreachable(set_row, port, reason, **extra):
    """The BMC didn't answer (version/power read failed, No route to host) — a
    DISTINCT state from a real flash `fault` (downgrade/mismatch/POST-failed). A
    BMC can be unreachable because it is activating, rebooting, hung, or off, so
    the operator action is 'wait or power-cycle', not 'the flash failed'. Do NOT
    pass current_version here: set_row merges, so any prior current_version is
    preserved — a node that DID reach the target shows 'unreachable' AT that
    version, never 'fault'."""
    set_row(port, phase="unreachable", fault_reason=reason, **extra)
    return "unreachable"


def _oem(set_row, port, version):
    """Terminal OEM state: a reachable Redfish BMC we deliberately do NOT flash (an
    OEM/AMI board with no flax-managed firmware). Distinct from `unreachable` (no
    answer) and `fault` (a real flash failure) — the board is fine, there is simply
    nothing to update. Shows its Redfish version so the operator sees what it runs."""
    set_row(port, phase="oem", current_version=version, target_version="",
            fault_reason="", percent=0)
    return "oem"


def _oem_version(client, matcher):
    """If the (dark-to-onetree) board is a reachable Redfish service root matching an
    updatable:false OEM manifest entry, return its Redfish version (or 'unknown').
    Else None — the caller falls back to `unreachable`. Uses the UNAUTHENTICATED
    service root (host-power independent; the AMI boards 401 our BMC creds)."""
    get_root = getattr(client, "get_redfish_root", None)
    if get_root is None:
        return None
    is_bmc, redfish_version, product = get_root()
    if not is_bmc or matcher.match_oem(product) is None:
        return None
    return redfish_version or "unknown"


def probe_one(port, client, matcher, set_row) -> str:
    """Read product+version, classify against the manifest, write the row. No flash."""
    product, _ = client.get_product_name()
    hit = matcher.match(product)
    if hit is None:
        return _fault(set_row, port, "unmanaged platform: %s" % (product or "unknown"))
    _platform, entry = hit
    target = manifest.target_version(entry)
    current, detail = client.get_fw_version(manifest.check_id(entry))
    if current is None:
        # onetree firmware-inventory unreadable. Before declaring unreachable, check
        # whether this is a reachable Redfish OEM board we deliberately don't flash
        # (a heterogeneous rack — braintree carries an AMI board among onetree ones).
        oem_ver = _oem_version(client, matcher)
        if oem_ver is not None:
            return _oem(set_row, port, oem_ver)
        # BMC dark (activating/rebooting/hung/off) — NOT a flash fault. Preserve any
        # prior current_version so a node that reached the target stays shown at it.
        return _unreachable(set_row, port, "BMC unreachable: %s" % detail,
                            target_version=target)
    try:
        cmp = manifest.compare(current, target)
    except ValueError as e:
        return _fault(set_row, port, str(e),
                      current_version=current, target_version=target)
    phase = "needs_update" if cmp == "older" else "up_to_date"
    set_row(port, phase=phase, current_version=current, target_version=target,
            fault_reason="", percent=0)
    return phase


def flash_one(port, client, matcher, fetch, set_row, share_base,
              *, sleep=time.sleep, max_wait_s=1200, poll_s=20) -> str:
    """Run the gate-only flash for one node. Returns the terminal phase."""
    # 1. checking
    product, _ = client.get_product_name()
    hit = matcher.match(product)
    if hit is None:
        return _fault(set_row, port, "unmanaged platform: %s" % (product or "unknown"))
    _platform, entry = hit
    target = manifest.target_version(entry)
    fw_id = manifest.check_id(entry)
    current, detail = client.get_fw_version(fw_id)
    if current is None:
        return _unreachable(set_row, port, "BMC unreachable: %s" % detail, target_version=target)
    set_row(port, phase="checking", current_version=current, target_version=target,
            fault_reason="", percent=0)
    try:
        cmp = manifest.compare(current, target)
    except ValueError as e:
        return _fault(set_row, port, str(e))
    if cmp == "same":
        set_row(port, phase="up_to_date")
        return "up_to_date"
    if cmp == "newer":
        return _fault(set_row, port,
                      "current %s is newer than target %s; refusing downgrade"
                      % (current, target))
    # cmp == "older" -> proceed to gate + flash

    # 2. gate — flash only a powered-ON (idle) host. Keeping the host NIC up lets
    # the BMC re-establish NC-SI cleanly after the activation reboot (an OFF host
    # is the suspected cause of post-activation BMC hangs). Operator powers nodes
    # on + idle before flashing; the driver never changes power itself.
    power, pdetail = client.get_power_state()
    if power is None:
        return _unreachable(set_row, port, "BMC unreachable (power read failed): %s" % pdetail)
    if power != "On":
        return _fault(set_row, port, "host powered %s; power on (idle) before flashing" % power)

    # 3. flashing
    try:
        image = fetch(share_base, manifest.artifact_rel_path(entry))
    except RuntimeError as e:
        return _fault(set_row, port, str(e))
    task, fdetail = client.post_flash(image, os.path.basename(manifest.artifact_rel_path(entry)))
    if task is None:
        return _fault(set_row, port, "flash POST failed: %s" % fdetail)
    set_row(port, phase="flashing", percent=0)

    # 4. monitoring — poll task percent until it goes dark (self-activation) or hits 100
    last_pct = 0
    waited = 0
    while waited <= max_wait_s:
        state, pct, _ = client.poll_task(task)
        if state is None:
            break                      # task endpoint dark -> BMC self-activating
        last_pct = pct
        set_row(port, phase="monitoring", percent=pct)
        if pct >= 100:
            break
        sleep(poll_s)
        waited += poll_s if poll_s else 1
    else:
        return _fault(set_row, port, "timed out during flash monitoring", percent=last_pct)

    # 5. activation-wait — the BMC self-reboots to apply (~13-15 min). It briefly
    # keeps answering at the OLD version, then goes dark (rebooting), then returns
    # at the new one. Wait for the version to CHANGE from the pre-flash baseline
    # (`current`), not merely to be readable — a stale read of the old version is
    # not success. Bounded by max_wait_s.
    waited = 0
    returned = None
    while waited <= max_wait_s:
        got, _ = client.get_fw_version(fw_id)
        if got is not None and got != current:
            returned = got
            break
        set_row(port, phase="activating", percent=99)
        sleep(poll_s)
        waited += poll_s if poll_s else 1
    if returned is None:
        return _unreachable(set_row, port,
                            "activation timeout — BMC unreachable; power-cycle to recover",
                            percent=last_pct)

    # 6. verifying
    try:
        if manifest.compare(returned, target) == "same":
            set_row(port, phase="done", current_version=returned, percent=100, fault_reason="")
            return "done"
    except ValueError as e:
        return _fault(set_row, port, str(e), percent=last_pct)
    return _fault(set_row, port, "post-flash version mismatch: %s" % returned,
                  current_version=returned, percent=last_pct)
