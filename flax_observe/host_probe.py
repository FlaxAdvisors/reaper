"""Host-side probe helpers — lifted from scripts/switchportrecond.py.

Functions:
  - lookup_lease_ip      — read /var/lib/misc/dnsmasq.leases
  - nginx_pxe_seen       — tail nginx access log for PXE GETs
  - inventory_status     — check /export/nodes/<nic_mac>/ for FRU dumps
  - ssh_uptime           — sshpass uptime check

In Plan 3 we still read /var/lib/misc/dnsmasq.leases — dnsmasq is still
serving DHCP. Plans 4-5 move DHCP to Kea on Postgres backend; the lease
lookup will rewire then.
"""
import datetime
import logging
import os
import subprocess

log = logging.getLogger("flax-observe.host_probe")

# ---------------------------------------------------------------------------
# Constants (mirrored from scripts/switchportrecond.py)
# ---------------------------------------------------------------------------

SSH_KNOWN_HOSTS = "/opt/flax/var/ssh/known_hosts"
SSH_TIMEOUT_SECS = 8

# ---------------------------------------------------------------------------
# DHCP lease lookup
# ---------------------------------------------------------------------------

def lookup_lease_ip(leases_path, mac, dhcp_hosts_dir=None):
    """Get the IP for a MAC from dnsmasq's view.

    Scans dnsmasq's active leases file first (live, dynamically-leased
    state). If the MAC is not there and dhcp_hosts_dir is provided, scans
    every regular file in that directory for static reservations of form
    'mac,ip,name' per line.

    Statically-configured BMCs (locally pinned to their reservation IP
    via the BMC web UI rather than DHCP) never appear in leases but do
    appear in dhcp-hosts, so checking both is necessary to resolve them.

    leases file format: '<expiry> <mac> <ip> <hostname> <client_id>'.
    dhcp-hosts file format: 'mac,ip[,name[,...]]' per line; '#' comments.

    Returns the IP string, or None if the MAC is in neither place.
    """
    if not mac:
        return None
    target = mac.lower()
    try:
        with open(leases_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1].lower() == target:
                    return parts[2]
    except FileNotFoundError:
        pass
    if dhcp_hosts_dir:
        try:
            entries = sorted(os.listdir(dhcp_hosts_dir))
        except (FileNotFoundError, NotADirectoryError):
            return None
        for entry in entries:
            path = os.path.join(dhcp_hosts_dir, entry)
            if not os.path.isfile(path):
                continue
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        fields = line.split(",")
                        if len(fields) >= 2 and fields[0].lower() == target:
                            # Pick the first dotted-decimal v4 from the
                            # remaining fields (handles MAC,IP,NAME and the
                            # MAC,set:tag,IP,NAME variant).
                            for fld in fields[1:]:
                                if "." in fld and fld.replace(".", "").isdigit():
                                    return fld
                            return fields[1]
            except OSError:
                continue
    return None


def lookup_kea_ip(pool, mac):
    """Resolve a MAC's v4 IP from Kea's Postgres backend.

    Replaces lookup_lease_ip's dnsmasq.leases/dhcp-hosts file scan now that
    Kea owns DHCP (Plan 5.6): those files are stale/removed. Checks the live
    lease (kea.lease4) first, then the reservation (kea.hosts) -- BMCs pinned
    to their reservation IP statically never appear in leases but do appear
    in kea.hosts. kea stores the v4 address as a host-order BIGINT; convert
    to a dotted-quad via host('0.0.0.0'::inet + addr). Returns the IP string,
    or None if the MAC is in neither table.
    """
    if not mac:
        return None
    mac_hex = mac.replace(":", "").replace("-", "").replace(".", "").lower()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT host(('0.0.0.0'::inet) + address) "
            "FROM kea.lease4 "
            "WHERE hwaddr = decode(%s, 'hex') AND state = 0 "
            "ORDER BY expire DESC LIMIT 1",
            (mac_hex,)).fetchone()
        if row and row[0]:
            return row[0]
        row = conn.execute(
            "SELECT host(('0.0.0.0'::inet) + ipv4_address) "
            "FROM kea.hosts "
            "WHERE dhcp_identifier = decode(%s, 'hex') "
            "AND dhcp_identifier_type = 0 AND ipv4_address IS NOT NULL "
            "LIMIT 1",
            (mac_hex,)).fetchone()
        if row and row[0]:
            return row[0]
    return None


def kea_lease_fresh(pool, mac, boundary_iso):
    """Has the node DHCP'd in the CURRENT boot session?

    True iff a kea.lease4 row for `mac` (state=0) has cltt >= boundary, where
    cltt = expire - valid_lifetime (the client's last DHCP transaction time).
    This is the node-side analogue of inventory_status's mtime-vs-boundary check:
    a stale lease from a prior boot (cltt < boundary) is NOT fresh, so nodeip
    shows 'waiting for DHCP' until the node actually requests its IP this boot.
    A reservation alone (kea.hosts) does NOT count -- that's an assignment, not
    a request. No boundary -> any active lease counts (mirrors inventory_status's
    `not link_changed_ts -> found`). No lease -> False.
    """
    if not mac:
        return False
    mac_hex = mac.replace(":", "").replace("-", "").replace(".", "").lower()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT expire, valid_lifetime FROM kea.lease4 "
            "WHERE hwaddr = decode(%s, 'hex') AND state = 0 "
            "ORDER BY expire DESC LIMIT 1",
            (mac_hex,)).fetchone()
    if not row or row[0] is None:
        return False
    expire, valid_lifetime = row[0], row[1]
    cltt = expire - datetime.timedelta(seconds=int(valid_lifetime or 0))
    if not boundary_iso:
        return True
    boundary = datetime.datetime.strptime(
        boundary_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return cltt >= boundary


# ---------------------------------------------------------------------------
# PXE observation
# ---------------------------------------------------------------------------

def nginx_pxe_seen(access_log_path, node_ip, boundary_iso=None, tail_lines=100):
    """Did node_ip fetch a liveiso payload (in the current boot session)?

    Filter-by-needle first, tail the most recent matches, check the IP as a
    substring (matching the bash predecessor `grep liveiso | tail | grep $ip`).
    When `boundary_iso` is given, a match counts only if its nginx log
    timestamp (`%d/%b/%Y:%H:%M:%S %z`) is >= the boundary -- so a fetch from a
    PRIOR boot (still in the log tail) does not show as this session's PXE.
    `boundary_iso=None` -> legacy behavior (timestamp ignored).
    """
    if not node_ip:
        return "unknown"
    needle = "/suse/live/test/LiveLeap"
    try:
        with open(access_log_path) as f:
            matches = [ln for ln in f if needle in ln]
    except FileNotFoundError:
        return "unknown"
    boundary = None
    if boundary_iso:
        boundary = datetime.datetime.strptime(
            boundary_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
    for line in matches[-tail_lines:]:
        if node_ip not in line:
            continue
        if boundary is None:
            return "found"
        ts = _nginx_line_ts(line)
        if ts is not None and ts >= boundary:
            return "found"
    return "notfound"


def _nginx_line_ts(line):
    """Parse the [dd/Mon/yyyy:HH:MM:SS +zzzz] timestamp from an nginx access
    line. Returns an aware datetime, or None if it can't be parsed."""
    lb, rb = line.find("["), line.find("]")
    if lb == -1 or rb == -1 or rb < lb:
        return None
    try:
        return datetime.datetime.strptime(
            line[lb + 1:rb], "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Inventory status
# ---------------------------------------------------------------------------

def inventory_status(nodes_root, nic_mac, link_changed_ts):
    """Has /export/nodes/post-<mac>/latest been collected in the CURRENT
    link session?

    Returns 'found' iff the file exists AND its mtime is newer than
    `link_changed_ts` (the linkstate var's `since` timestamp, which only
    advances on link-state transitions — not on every poll). 'notfound'
    if the file is missing or predates the current link session;
    'unknown' if there's no NIC MAC yet.

    The mtime-vs-link-ts comparison is load-bearing, NOT a freshness
    heuristic: linkstate.since marks when the current 'link' value was
    established (i.e. when the port came up). If the inventory file
    predates that, the file was written during a PRIOR link session and
    there's been at least one link-down->link-up transition since. A
    reslot (or any physical re-insertion that preserves the NIC MAC but
    changes the underlying chassis — same MAC swapped into a different
    machine, NIC card moved, etc.) is invisible to the bmc_mac /
    chassis_sn signals when only the NIC end is replaced, so this
    timestamp check is the only thing that forces re-inventory after a
    link-session boundary. Don't soften it.
    """
    if not nic_mac:
        return "unknown"
    fn = "post-" + nic_mac.replace(":", "")
    latest = os.path.join(nodes_root, fn, "latest")
    try:
        mtime = os.path.getmtime(latest)
    except OSError:
        return "notfound"
    if not link_changed_ts:
        return "found"
    link_ts = datetime.datetime.strptime(
        link_changed_ts, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=datetime.timezone.utc).timestamp()
    return "found" if mtime > link_ts else "notfound"


# ---------------------------------------------------------------------------
# SSH uptime probe
# ---------------------------------------------------------------------------

def ssh_uptime(ip, host_creds, timeout=SSH_TIMEOUT_SECS):
    """ssh-runs `uptime` walking host_creds; returns 'ok' / 'fail' / 'unknown'.

    Walks a list of {user, pass} dicts (loaded from
    /etc/flax/credentials-host.json — same shape reaper-leased uses).
    First credential whose ssh returns rc=0 -> "ok". All tried + none
    succeeded -> "fail". No ip or no creds -> "unknown".

    Mirrors reaper-leased's host-cred walk pattern. The bash predecessor
    used a single sshuser/sshpass tuple from credentials.json; modern
    deployments split that into a walkable list under credentials-host.json
    so a node provisioned with a non-default OS (different default user)
    can still be probed without per-site config drift.
    """
    if not ip:
        return "unknown"
    if not host_creds:
        return "unknown"
    tried_any = False
    for c in host_creds:
        try:
            cp = subprocess.run(
                ["sshpass", "-p", c["pass"], "ssh",
                 "-tt",
                 "-o", "StrictHostKeyChecking=no",
                 "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
                 "-o", f"ConnectTimeout={timeout}",
                 "-l", c["user"], ip, "uptime"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout + 2,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            tried_any = True
            continue
        tried_any = True
        if cp.returncode == 0:
            return "ok"
    return "fail" if tried_any else "unknown"
