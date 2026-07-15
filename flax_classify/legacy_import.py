# flax_classify/legacy_import.py
"""One-shot import of live dnsmasq reservations into kea.hosts (Plan 5.6).

The Kea cutover serves DHCP from kea.hosts. flax-classify only derives the
subset it can recompute from observe_state; reaper-leased's
/etc/dnsmasq.dhcp-hosts/* and /etc/dnsmasq.d/bmc-reservations.conf hold
more (VMs, turtle ports, bang BMCs). This module parses those dnsmasq
dhcp-host lines and upserts every MAC->IP(+hostname) into kea.hosts
tagged user_context.source='legacy-import' so it is never evicted: the
materializer's planner treats an unowned legacy-import row as NOT OURS
(materializer.py's unowned-source rule -- it never plans a delete for one),
and kea_hosts.py's upsert ON CONFLICT CASE guard protects an existing row's
source tag from being clobbered by a different source's re-upsert. (The
former sweep, kea_hosts.delete_stale_kea_hosts, was retired in the phase-3b
demolition -- see that module's docstring point 4.)

Run inside the flax-control container during cutover:
    python -m flax_classify.legacy_import import  <file> [<file> ...]
    python -m flax_classify.legacy_import coverage <file> [<file> ...]
The DB connection comes from flax_classify.db.build_pool (same env as the
service).
"""
import re
import sys
from pathlib import Path

from .kea_hosts import _mac_hex

# dnsmasq mgmt reservations (192.168.88.0/24) map to the Kea mgmt subnet id.
MGMT_SUBNET_ID = 4

_MAC_RE = re.compile(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b")
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _vid_for_ip(ip: str) -> int:
    """Derive the Kea subnet id from a reservation IP.
    172.<vid>.x.y -> vid; 192.168.88.x -> MGMT_SUBNET_ID."""
    octs = ip.split(".")
    if octs[0] == "172":
        return int(octs[1])
    if ip.startswith("192.168.88."):
        return MGMT_SUBNET_ID
    raise ValueError(f"cannot map IP to subnet: {ip}")


def parse_dhcp_host_lines(text: str) -> list[dict]:
    """Parse dnsmasq reservations in BOTH formats:

      1. conf-file / dnsmasq.conf form:  ``dhcp-host=mac,ip[,name][,set:tag]``
      2. dhcp-hostsdir form (bare):      ``mac,ip[,name][,set:tag]``

    reaper-leased writes the BARE form into /etc/dnsmasq.dhcp-hosts/* (the
    bulk of reservations), while /etc/dnsmasq.d/bmc-reservations.conf uses
    the ``dhcp-host=`` form. We must capture both or the coverage gate
    (rightly) fails.

    Tolerates set:/id:/tag: prefixes, lease-time suffixes (1h/45m/30s/3600/
    infinite), comments, and unrelated lines (interface=, etc.). Hostname
    is the last comma field that is none of those."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("dhcp-host="):
            fields = line[len("dhcp-host="):].split(",")
        elif _MAC_RE.fullmatch(line.split(",")[0].strip()):
            # bare dhcp-hostsdir line whose first field is a MAC
            fields = line.split(",")
        else:
            continue
        mac = ip = None
        hostname = ""
        for f in fields:
            f = f.strip()
            if _MAC_RE.fullmatch(f):
                mac = f.lower()
            elif _IPV4_RE.fullmatch(f):
                ip = f
            elif f.startswith(("set:", "id:", "tag:")) or f == "":
                continue
            elif re.fullmatch(r"\d+[hms]?", f) or f == "infinite":
                continue  # dnsmasq lease-time suffix, not a hostname
            else:
                hostname = f  # last non-structured token wins
        if not mac or not ip:
            continue
        out.append({"mac": mac, "ipv4_address": ip,
                    "hostname": hostname, "vid": _vid_for_ip(ip)})
    return out


_IMPORT_SQL = """
INSERT INTO kea.hosts
    (dhcp_identifier, dhcp_identifier_type, ipv4_address, hostname,
     dhcp4_subnet_id, user_context)
VALUES
    (decode(%(mac_hex)s, 'hex'),
     0,
     (%(ipv4_address)s::inet - inet '0.0.0.0')::bigint,
     %(hostname)s,
     %(dhcp4_subnet_id)s,
     jsonb_build_object('source', %(source)s::text)::text)
ON CONFLICT (dhcp_identifier, dhcp_identifier_type, dhcp4_subnet_id)
  WHERE dhcp4_subnet_id IS NOT NULL
  DO UPDATE SET
    ipv4_address = EXCLUDED.ipv4_address,
    hostname     = EXCLUDED.hostname,
    user_context = (
        COALESCE(kea.hosts.user_context, '{}')::jsonb
        || jsonb_build_object('source', %(source)s::text)
    )::text
"""


def import_reservations(pool, entries: list[dict]) -> int:
    """Upsert each entry into kea.hosts tagged source='legacy-import'.
    Returns count imported. Idempotent (ON CONFLICT)."""
    n = 0
    with pool.connection() as conn:
        for e in entries:
            try:
                conn.execute(_IMPORT_SQL, {
                    "mac_hex": _mac_hex(e["mac"]),
                    "ipv4_address": e["ipv4_address"],
                    "hostname": e.get("hostname", ""),
                    "dhcp4_subnet_id": e["vid"],
                    "source": "legacy-import",
                })
            except Exception as exc:
                raise RuntimeError(
                    f"failed importing {e['mac']} -> {e['ipv4_address']}: {exc}"
                ) from exc
            n += 1
    return n


# One-shot cutover remediation (the inverse of over-capture). legacy_import
# tags EVERY parsed dnsmasq reservation source='legacy-import' so the
# materializer never evicts it -- correct for rows flax cannot recompute (VMs,
# turtle ports, bang/switch BMCs), WRONG for a mac flax DOES own once a
# desired_reservations row exists for it. Eindhoven never hit this: its engines
# wrote kea.hosts with source=post/triage during phases 1-2, so by materializer
# cutover the flax-owned macs already carried a role source and only the truly-
# external rows stayed legacy-import. A site migrated STRAIGHT into the enforce
# architecture (engines write desired_reservations only -- braintree) has no
# such pre-tagging: every flax-owned mac is still legacy-import, which the
# materializer's ownership rule treats as permanently NOT-OURS (skipped_unowned,
# never upserted), so kea.hosts can never converge onto desired. adopt fixes
# that by re-tagging exactly the legacy-import rows a desired row now claims to
# their desired owner_role -- leaving the no-desired-overlap rows (still truly
# external) untouched. The next materializer cycle then attributes them as owned
# and upserts any drifted fields (e.g. the stale pre-cutover hostname).
# Idempotent: after the first run no legacy-import row overlaps desired, so a
# re-run updates 0. Eindhoven-safe by construction: with no legacy-import<->
# desired overlap there, it updates 0 rows.
#
# RAW string (r"""...""") -- mandatory, exactly like _POST_MAC_SQL in
# kea_hosts.py: the E'\1:\2...' replacement must reach Postgres with LITERAL
# backslashes so the E-string yields regexp backreferences \1..\6. In a plain
# (non-raw) Python literal, "\\1" collapses to "\1", which Postgres then reads
# as an OCTAL escape (a control char), not a backreference -- the join silently
# matches 0 rows. (A .sql-file dry-run hides this: psql keeps the doubled
# backslash, so only the in-process code path exercises the Python string layer.)
_ADOPT_SQL = r"""
UPDATE kea.hosts h
SET user_context = (
        COALESCE(h.user_context, '{}')::jsonb
        || jsonb_build_object('source', d.owner_role)
    )::text
FROM desired_reservations d
WHERE h.dhcp_identifier_type = 0
  AND regexp_replace(encode(h.dhcp_identifier, 'hex'),
        '(..)(..)(..)(..)(..)(..)', E'\\1:\\2:\\3:\\4:\\5:\\6') = lower(d.mac)
  AND (h.user_context::jsonb) ->> 'source' = 'legacy-import'
"""


def adopt_owned_legacy_rows(pool) -> int:
    """Re-tag legacy-import kea.hosts rows a desired_reservations row now owns,
    setting user_context.source to that row's owner_role. Scoped to the
    legacy-import<->desired overlap only (see _ADOPT_SQL); idempotent; returns
    the number of rows re-tagged."""
    with pool.connection() as conn:
        cur = conn.execute(_ADOPT_SQL)
        return cur.rowcount


def coverage_gap(pool, entries: list[dict]) -> set[str]:
    """Return the set of captured mac-hex strings NOT present in kea.hosts.
    Empty set == every captured reservation is covered (cutover-safe)."""
    want = {_mac_hex(e["mac"]) for e in entries}
    with pool.connection() as conn:
        cur = conn.execute(
            "SELECT dhcp_identifier FROM kea.hosts WHERE dhcp_identifier_type = 0")
        have = {row[0].hex() for row in cur.fetchall()}
    return want - have


def _main(argv):
    from .__main__ import _build_conninfo
    from .db import build_pool
    # adopt takes NO file args (it reads desired_reservations, not dnsmasq).
    if argv and argv[0] == "adopt":
        pool = build_pool(_build_conninfo())
        n = adopt_owned_legacy_rows(pool)
        print(f"adopted {n} legacy-import reservation(s) into their "
              f"desired owner_role")
        return 0
    if len(argv) < 2 or argv[0] not in ("import", "coverage"):
        print("usage: python -m flax_classify.legacy_import "
              "{import|coverage} <file> [<file> ...]\n"
              "       python -m flax_classify.legacy_import adopt",
              file=sys.stderr)
        return 2
    cmd, paths = argv[0], argv[1:]
    parts = []
    for p in paths:
        with open(p) as fh:
            parts.append(fh.read())
    text = "\n".join(parts)
    entries = parse_dhcp_host_lines(text)
    pool = build_pool(_build_conninfo())
    if cmd == "import":
        n = import_reservations(pool, entries)
        print(f"imported {n} reservations ({len(entries)} parsed)")
        return 0
    gap = coverage_gap(pool, entries)
    if gap:
        print(f"COVERAGE GAP: {len(gap)} mac(s) not in kea.hosts: "
              f"{sorted(gap)}", file=sys.stderr)
        return 1
    print(f"coverage OK: all {len(entries)} captured reservations present")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
