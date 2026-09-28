"""Observe identity hold + serial confirmation (spec 2026-09-27 §4, piece B).

A BMC reboot empties the port's FDB for minutes (12 V cut: ~3.5 min) while
the blade never leaves its slot; observe used to write bmc_mac=None and the
triage reservation was swept. And when a DIFFERENT BMC mac appears on a port
whose chassis serial is latched, it is either the same blade with a flipped
BMC MAC or a different blade -- decided here by reading the new mac's chassis
serial (EUI-64 IPv6 link-local reach, the family's serial reader).

Pure decision (`decide`) + two helpers; state_machine.port_worker_one_iter
applies the outcome. No I/O happens here except through the callables passed
in.
"""
HOLD_SECS = 900
PROBE_RETRY_SECS = 60

LIVE = "live"          # use this cycle's verdict as-is
HELD = "held"          # keep the latched identity; the BMC is not visible
EXPIRED = "expired"    # hold ran out: today's path (verdict as-is)
FLIPPED = "flipped"    # same serial, new BMC mac: adopt it, keep the serial
SWAPPED = "swapped"    # different blade (or unconfirmed at expiry): forget


def decide(*, latched_mac, chassis_sn, new_mac, hold_age, seen_elsewhere,
           serial_of_new, hold_secs=HOLD_SECS):
    """Outcome for one cycle. `latched_mac` is the BMC mac the latched
    chassis_sn belongs to; `new_mac` is this cycle's confirm_roles bmc_mac;
    `hold_age` seconds since the hold began (0 when it starts now)."""
    if not latched_mac or not chassis_sn:
        return LIVE
    if new_mac == latched_mac:
        return LIVE
    in_window = hold_age < hold_secs
    if not new_mac or seen_elsewhere:
        return HELD if in_window else EXPIRED
    if serial_of_new:
        return FLIPPED if serial_of_new.strip() == chassis_sn.strip() else SWAPPED
    return HELD if in_window else SWAPPED


def memo_serial(port_state, mac, reader, *, now_iso, secs_since):
    """reader(mac) at most once per mac while it answers, and at most every
    PROBE_RETRY_SECS while it does not (R2). The memo lives in
    port_state["identity_probe"] = {"mac", "at", "serial"}."""
    memo = port_state.get("identity_probe") or {}
    if memo.get("mac") == mac:
        if memo.get("serial"):
            return memo["serial"]
        if secs_since(memo.get("at")) < PROBE_RETRY_SECS:
            return None
    serial = reader(mac)
    port_state["identity_probe"] = {"mac": mac, "at": now_iso, "serial": serial}
    return serial


def read_serial_for_mac(mac, *, reach, cached_probe, probe_kind, serial_via_ssh,
                        serial_openbmc, serial_traditional, serial_read,
                        bmc_creds, confirmed_kinds):
    """Chassis serial of the BMC answering for `mac`, or None.

    reach(mac) -> (target, is_ll); the kind probe from this cycle's gather is
    reused when present (cached_probe). Only a confirmed BMC kind is asked for
    a serial -- a lone host NIC MAC (labelled BMC by the single-MAC path while
    the real BMC reboots) never is. IPMI does not work over a link-local
    address, so the LAN reader needs an IPv4 target."""
    target, is_ll = reach(mac)
    if not target:
        return None
    probe = cached_probe or probe_kind(target)
    kind = (probe or {}).get("kind")
    creds = (probe or {}).get("creds_used")
    if kind not in confirmed_kinds or not creds:
        return None
    vendor = probe.get("vendor")
    if serial_via_ssh(vendor, kind):
        sn, state = serial_read(serial_openbmc(target, creds))
        if state == "ok" and sn:
            return sn
        if state not in ("absent", "error") or is_ll:
            return None
        for c in bmc_creds:
            sn, state = serial_read(serial_traditional(
                target, (c["bmcuser"], c["bmcpass"])))
            if state == "ok" and sn:
                return sn
        return None
    if is_ll:
        return None
    sn, state = serial_read(serial_traditional(target, creds))
    return sn if state == "ok" and sn else None
