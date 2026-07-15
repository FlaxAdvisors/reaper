"""3-rung DUT-kick ladder, lifted from scripts/reaper_leased.py.

Rungs: bmc_ll (IPv6-LL ssh) -> host_ssh -> switch_flap. Only switch_flap is
disruptive; it writes a Postgres intentional_flap sentinel BEFORE bouncing the
port so flax-observe freezes that port's state.

The EUI-64/ping/ssh helpers (_eui64_ll_from_mac, _ping6_reachable,
_default_ssh_runner, force_dut_redhcp) and their transitive deps (normalise_mac,
SSH_KNOWN_HOSTS, _KICK_CMD) are COPIED VERBATIM from scripts/reaper_leased.py.
They are NOT imported: the flax-control Dockerfile copies the flax_* packages and
schema/ into the image but NOT scripts/, so `from scripts.reaper_leased import ...`
would fail at runtime. Do NOT reimplement the EUI-64 derivation — it is a
byte-for-byte copy.
"""
import logging
import os
import subprocess

from . import db, sentinel

log = logging.getLogger("flax-reconcile.kick")

# --- helpers lifted VERBATIM from scripts/reaper_leased.py -------------------
# Shared SSH known_hosts file (reaper_leased.py SSH_KNOWN_HOSTS, INSTALL_ROOT).
INSTALL_ROOT = "/opt/flax"
SSH_KNOWN_HOSTS = os.path.join(INSTALL_ROOT, "var", "ssh", "known_hosts")


def normalise_mac(mac):
    """Normalise to colon-lower hex. Accepts dashes (Windows-style), mixed case."""
    return mac.replace("-", ":").replace(".", ":").lower()


def _eui64_ll_from_mac(mac):
    """Compute the IPv6 link-local EUI-64 address from a MAC, in canonical
    compressed form (single :: collapses the longest zero run, matching
    what `ip -6 addr` prints)."""
    import ipaddress
    parts = normalise_mac(mac).split(":")
    if len(parts) != 6:
        raise ValueError("bad mac: " + repr(mac))
    first = int(parts[0], 16) ^ 0x02
    octets = [first.to_bytes(1, "big").hex()] + parts[1:3] + ["ff", "fe"] + parts[3:6]
    full = "fe80::" + ":".join(
        "".join(octets)[i:i + 4] for i in range(0, 16, 4))
    return str(ipaddress.IPv6Address(full))


def _ping6_reachable(addr, timeout=2, runner=None):
    """True iff ping6 to addr (possibly with %iface scope) gets at least one
    reply within `timeout` seconds. `runner` is an injectable
    (addr, timeout) -> bool callable for tests."""
    if runner is not None:
        return runner(addr, timeout)
    try:
        r = subprocess.run(
            ["ping6", "-c", "1", "-W", str(int(timeout)), addr],
            timeout=timeout + 1,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False)
        return r.returncode == 0
    except Exception:
        return False


def _default_ssh_runner(host, user, password, cmd):
    # 25s timeout: openbmc's ipmitool can take 8-15s on slow BMC processors
    # (Phosphor distros on AST2400/2500 BMCs). ConnectTimeout=3 keeps
    # unreachable hosts fast (3s); reachable hosts get the full budget for
    # the actual command. Tested empirically on a vid-17 tiogapass BMC.
    full = ["sshpass", "-p", password, "ssh",
            "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
            "-o", "ConnectTimeout=3",
            "-o", "ServerAliveInterval=5",
            f"{user}@{host}", cmd]
    return subprocess.check_output(full, timeout=25, text=True,
                                   stderr=subprocess.DEVNULL)


# Forces a DUT's DHCP client to release and re-acquire. Modern netplan +
# systemd-networkd hosts don't ship dhclient and don't release leases on
# carrier-loss, so a switch port flap alone won't move them — we have to
# tell the host directly. networkctl renew re-runs DHCP on the named
# iface (reload only re-reads configs and does NOT renew leases); dhclient
# is the fallback for older distros. The interface is discovered at run
# time from the default route — DUTs use a mix of names (br0, ens7f0,
# eno1, …) so hardcoding one drops most of them on the floor. Output
# line "KICK=<method>" tells the daemon which path was taken (logged in
# the dut_kicked event). Requires passwordless sudo for the chosen tool
# — flax is normally sudoers on lab DUTs.
_KICK_CMD = (
    "iface=$(ip route show default 2>/dev/null "
    "| awk '/default/ {print $5; exit}'); "
    "if [ -z \"$iface\" ]; then echo KICK=none; "
    "elif command -v networkctl >/dev/null 2>&1; then "
    "  sudo -n networkctl renew \"$iface\" && echo KICK=networkctl; "
    "elif command -v dhclient >/dev/null 2>&1; then "
    "  sudo -n dhclient -r \"$iface\" 2>/dev/null; "
    "  sudo -n dhclient \"$iface\" && echo KICK=dhclient; "
    "else echo KICK=none; fi"
)


def force_dut_redhcp(ip, host_creds, ssh_runner=None):
    """SSH to a DUT and force its DHCP client to release+re-acquire so it
    picks up the dnsmasq reservation we just wrote. Tries each cred in
    host_creds until one authenticates; on success runs _KICK_CMD which
    prefers networkctl (systemd-networkd / netplan), falls back to
    dhclient. Returns the method string ('networkctl', 'dhclient',
    'none') reported by the kick command, or 'failed' if no cred
    authenticated."""
    if ssh_runner is None:
        ssh_runner = _default_ssh_runner
    for c in host_creds:
        try:
            out = ssh_runner(ip, c["user"], c["pass"], _KICK_CMD)
        except Exception:
            continue
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("KICK="):
                return line.split("=", 1)[1].strip()
        return "unknown"
    return "failed"


# Forces an OpenBMC's eth0 to RELEASE its current DHCP lease and re-DISCOVER, so
# a BMC sitting on a stale pool lease converges onto its reservation. A bare
# `ip link set eth0 down/up` does NOT do this on a systemd-networkd / Phosphor
# BMC: networkd keeps the lease across carrier loss and only RENEWs the held
# address on carrier-up, so the device never moves (same failure as a switch
# flap — see cycle.py). The fix is to drop the cached networkd lease so the next
# DHCP is a fresh DISCOVER (Kea then offers the reservation), then reconfigure.
# Stack-adaptive: systemd-networkd (networkctl) -> ifupdown (older OpenBMC, e.g.
# Bryce Canyon) -> raw `ip link` as a last resort. Runs detached (nohup &) by
# the caller because bouncing eth0 drops the SSH session mid-command.
_BMC_KICK_CMD = (
    "ifx=$(cat /sys/class/net/eth0/ifindex 2>/dev/null); "
    "if command -v networkctl >/dev/null 2>&1; then "
    "  rm -f /run/systemd/netif/leases/$ifx; "
    "  networkctl reconfigure eth0 || systemctl restart systemd-networkd; "
    "elif command -v ifdown >/dev/null 2>&1; then ifdown eth0 && ifup eth0; "
    "else ip link set eth0 down; sleep 1; ip link set eth0 up; fi"
)


# --- the 3-rung ladder ------------------------------------------------------
def kick_via_bmc_ll(*, mac, iface, obmc_user, obmc_pass,
                    ssh_runner=None, ping_runner=None):
    if not (obmc_user and obmc_pass):
        return False
    ll = _eui64_ll_from_mac(mac)
    addr = ll + "%" + iface
    if not _ping6_reachable(addr, timeout=2, runner=ping_runner):
        return False
    runner = ssh_runner or _default_ssh_runner
    try:
        runner(addr, obmc_user, obmc_pass,
               "nohup sh -c '" + _BMC_KICK_CMD + "' >/dev/null 2>&1 &")
        return True
    except Exception:
        return False


def kick_via_host_ssh(*, ip, host_creds, ssh_runner=None):
    if not (ip and host_creds):
        return False
    try:
        return force_dut_redhcp(ip, host_creds, ssh_runner=ssh_runner) != "failed"
    except Exception:
        return False


def kick_via_switch_flap(*, pool, switches, sw_name, port, kind="host",
                         reason="", mac=None, hold_seconds=2):
    sw = switches.get(sw_name)
    if sw is None or not port:
        return False
    try:
        sentinel.write_sentinel(pool, switch=sw_name, port=port,
                                hold_seconds=hold_seconds,
                                reason=reason or "unspecified", mac=mac)
    except Exception:
        # Losing the freeze signal is bad but not as bad as losing the kick;
        # observe will just record the flap as a normal transition.
        log.warning("sentinel write failed for %s/%s; flapping anyway", sw_name, port)
    # Mark the flap in-flight BEFORE the shutdown so that if the process dies
    # between flap()'s shutdown and no-shutdown batches the startup self-heal
    # (or the SIGTERM handler) un-strands the port. Cleared after flap() returns.
    try:
        db.mark_flap_pending(pool, switch=sw_name, port=port, mac=mac)
    except Exception:
        log.warning("flap-pending mark failed for %s/%s; flapping anyway",
                    sw_name, port)
    try:
        try:
            sw.flap(port, hold_seconds=hold_seconds)
        except TypeError:
            sw.flap(port)
    except Exception:
        # Leave the flap-pending marker in place on failure: if the shutdown
        # landed but the no-shutdown did not, the port may be stranded
        # admin-down, and the startup self-heal will re-issue the no-shutdown.
        # A still-reachable port simply gets a harmless no-op `no shutdown` on
        # the next recovery pass.
        return False
    # Success: the no-shutdown completed, so the marker has served its purpose.
    try:
        db.clear_flap_pending(pool, switch=sw_name, port=port)
    except Exception:
        log.warning("flap-pending clear failed for %s/%s", sw_name, port)
    return True


def _iface_for_vid(vid, vlan_parents=None):
    """Return the sub-interface name for a VLAN ID.

    The parent interface comes from vlans.json (vid → parent), loaded by the
    entrypoint (__main__) and passed down through run_ladder as vlan_parents.
    "eth0" is only a fallback for entries missing from the map; callers should
    always supply the full vlan_parents dict.
    """
    parent = (vlan_parents or {}).get(int(vid), "eth0")
    return parent + "." + str(int(vid))


def run_ladder(*, pool, switches, mac, kind, switch, port, vid, target_ip,
               obmc_user, obmc_pass, host_creds, flap_hold_seconds, reason,
               vlan_parents=None):
    """Pick the class-appropriate rung and execute it, falling through on
    failure. Returns (rung_name, ok). Mirrors reaper_leased.kick().

    vlan_parents maps vid (int) → parent iface name (str); loaded from
    vlans.json by the entrypoint and threaded down here so the bmc_ll rung
    can probe the correct host interface (e.g. eth1 on eindhoven, not eth0).
    """
    if kind == "bmc" and vid:
        if kick_via_bmc_ll(mac=mac, iface=_iface_for_vid(vid, vlan_parents),
                           obmc_user=obmc_user, obmc_pass=obmc_pass):
            return ("bmc_ll", True)
    elif kind == "host" and target_ip:
        if kick_via_host_ssh(ip=target_ip, host_creds=host_creds):
            return ("host_ssh", True)
    if switch and port:
        if kick_via_switch_flap(pool=pool, switches=switches, sw_name=switch,
                                port=port, kind=kind or "host", reason=reason,
                                mac=mac, hold_seconds=flap_hold_seconds):
            return ("switch_flap", True)
    return (None, False)
