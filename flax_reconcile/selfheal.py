"""Interrupted-flap self-heal: un-strand ports a killed flap left admin-down.

drivers.flap() is `shutdown` -> sleep -> `no shutdown` in two separate eAPI/ssh
batches. If the process dies between them (SIGKILL on `systemctl stop`, crash,
restart) the port is stranded admin-down (`disabled` in `show interface status`)
and the DUT goes unreachable. On 2026-06-18 a flap storm + an abrupt stop left 8
rabbit-lorax ports disabled, recovered by hand.

recover_stranded_ports() runs once at startup (before the sweep loop) and is
also reused by the SIGTERM handler. Two signals, in precedence order:

  1. flap_pending markers (PRECISE): db.mark_flap_pending writes a row BEFORE
     every flap's shutdown and db.clear_flap_pending deletes it after the
     no-shutdown completes. Any row still present is a port reconcile was
     mid-flapping -> re-issue set_admin_up + clear it. This is the load-bearing
     signal because the persisted switch_facts.link field collapses admin-down
     ('disabled') into 'nolink' together with a genuinely absent device, so a
     facts scan alone cannot tell them apart.

  2. admin-down access-port scan (FALLBACK): for any managed DUT access port
     that reads not-up in switch_facts AND is a port reconcile manages (has a
     desired_port row or resolves to a device location) AND is NOT a
     trunk/uplink and NOT no-steer-listed, re-issue set_admin_up. On a port that
     is already up or genuinely empty this is a harmless no-op `no shutdown`; we
     never touch trunk/uplink, no-steer, or non-managed ports so we don't fight
     an operator who intentionally disabled something.

Best-effort per port: a SwitchUnreachable / any exception on one port is logged
and skipped; the pass continues and returns the count actually recovered.

py3.11 note: string building uses + concatenation, no backslashes inside
f-string {} expressions (bang-fiesta runs Python 3.11).
"""
import json
import logging

from . import db

log = logging.getLogger("flax-reconcile.selfheal")


def _audit_recovered(pool, *, switch, port, mac, via):
    """Write one audit.events row per recovered port (operator visibility).

    Mirrors cycle._emit_fault's direct INSERT (service='flax-reconcile', the
    audit.events columns from migration 004) with kind='port_recovered'.
    """
    try:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO audit.events "
                "(service, kind, mac, switch, port, payload) "
                "VALUES ('flax-reconcile', 'port_recovered', %s, %s, %s, %s)",
                (mac, switch, port,
                 json.dumps({"reason": "un-stranded a port a killed flap left "
                             "admin-down", "via": via})))
    except Exception:
        log.warning("port_recovered audit write failed for %s/%s", switch, port)


def _set_admin_up(driver, port):
    """Call driver.set_admin_up(port); return True on success, False on any
    exception (SwitchUnreachable or otherwise). Best-effort per port."""
    try:
        driver.set_admin_up(port)
        return True
    except Exception as e:  # SwitchUnreachable + anything else
        log.warning("set_admin_up failed for %s: %s", port, e)
        return False


def recover_stranded_ports(pool, switches, *, no_steer=None) -> int:
    """Un-strand ports a killed flap left admin-down. Returns count recovered.

    Args:
        pool:      psycopg ConnectionPool.
        switches:  {name -> driver} (AristaEAPI / IOS / Cumulus). Only ports on
                   a switch present here can be recovered.
        no_steer:  iterable of (switch, port) hard-excluded pairs; never touched.

    Order: precise flap_pending markers first, then the admin-down fallback
    scan (skipping any (switch, port) already handled via a marker).
    """
    no_steer = set(no_steer or set())
    recovered = 0
    handled = set()  # (switch, port) already recovered, so the scan skips them

    # ---- 1. precise: stale flap_pending markers --------------------------
    try:
        pending = db.read_flap_pending(pool)
    except Exception as e:
        log.warning("read_flap_pending failed: %s", e)
        pending = []
    for row in pending:
        sw_name, port, mac = row["switch"], row["port"], row.get("mac")
        driver = switches.get(sw_name)
        if driver is None:
            # Switch no longer managed -- drop the stale marker so it doesn't
            # linger forever.
            _safe_clear(pool, sw_name, port)
            continue
        if (sw_name, port) in no_steer:
            _safe_clear(pool, sw_name, port)
            continue
        if _set_admin_up(driver, port):
            recovered += 1
            handled.add((sw_name, port))
            log.info("self-heal: re-enabled %s/%s (flap_pending marker)",
                     sw_name, port)
            _audit_recovered(pool, switch=sw_name, port=port, mac=mac,
                             via="flap_pending")
            _safe_clear(pool, sw_name, port)
        # On failure leave the marker so the next startup retries.

    # ---- 2. fallback: admin-down managed access-port scan ----------------
    try:
        sf_ports = db.read_switch_facts_ports(pool)
    except Exception as e:
        log.warning("read_switch_facts_ports failed: %s", e)
        return recovered
    try:
        desired = {(d["switch"], d["port"]) for d in db.read_desired_ports(pool)}
    except Exception as e:
        log.warning("read_desired_ports failed: %s", e)
        desired = set()

    for (sw_name, port), fact in sf_ports.items():
        if (sw_name, port) in handled:
            continue
        driver = switches.get(sw_name)
        if driver is None:
            continue
        if (sw_name, port) in no_steer:
            continue
        # Only ACCESS ports reconcile manages: a trunk/uplink mask is excluded,
        # and a non-managed port (no desired_port row AND no device location) is
        # left alone so we never fight an operator-disabled infra port.
        if fact.get("mask") != "access":
            continue
        if not db.port_admin_down(fact):
            continue  # already up (or genuinely link-up) -> nothing to do
        managed = (sw_name, port) in desired or _resolves_to_device(
            pool, sw_name, port)
        if not managed:
            continue
        if _set_admin_up(driver, port):
            recovered += 1
            handled.add((sw_name, port))
            log.info("self-heal: re-enabled %s/%s (admin-down access scan)",
                     sw_name, port)
            _audit_recovered(pool, switch=sw_name, port=port, mac=None,
                             via="admin_down_scan")

    return recovered


def _resolves_to_device(pool, switch, port) -> bool:
    """True iff some device's location resolves to this (switch, port).

    Reuses the devices table (the same source resolve_location reads) so the
    fallback scan only touches a port a DUT is actually assigned to.
    """
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM devices WHERE switch = %s AND port = %s LIMIT 1",
                (switch, port)).fetchone()
        if row is not None:
            return True
        # devices.port may be stored in internal short form; try the canonical
        # form too via a join-free second lookup is overkill -- desired_port
        # already covers the canonical-named managed ports, so a miss here is
        # acceptable (the marker path is the precise recovery).
        return False
    except Exception:
        return False


def _safe_clear(pool, switch, port):
    try:
        db.clear_flap_pending(pool, switch=switch, port=port)
    except Exception:
        log.warning("flap-pending clear failed for %s/%s", switch, port)
