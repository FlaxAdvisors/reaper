"""kea.hosts reservations for customer-rack orders (rackpost), `order:*` only.

rackpost (flax_rack, on the bang) never writes Kea itself; it calls the three
/api/v1/order-reservations routes, which land here. This is the one place
flax-control INSERTs into kea.hosts, and it is fenced to rows whose
user_context.source matches SOURCE_RE (`order:RRORD-101`, `order:RRORD-101/b`
for rack b of a multi-rack order). Every other row is someone else's:

  - upsert refuses (Conflict, nothing written) when the MAC already has a row
    under ANY other source -- triage, post, manual, legacy-import, an untagged
    legacy row, or a sibling rack of the same order -- or when the IP is held
    by another identifier in the same subnet. The checks and the write share
    one transaction, and the ON CONFLICT DO UPDATE carries its own source guard,
    so a row that turned foreign between check and write is still not touched.
  - delete_source / list_source match the source by equality, never a prefix,
    so closing rack "" of an order never touches rack "/b".

The flax-classify materializer attributes an unknown source as NOT OURS and
never plans against it, so these rows are left alone by the core cycle (see
flax-storage-delta.md). v4 only: no dhcp6_subnet_id, no ipv6_reservations row.
Grants: migration 036 (INSERT, UPDATE + sequence); DELETE is migration 022's.

Own SQL on purpose -- flax_control does not import flax_classify. The INSERT
mirrors flax_classify/kea_hosts.py _UPSERT_SQL's partial-index ON CONFLICT.
"""
import ipaddress
import json
import re

from .db import get_pool

SOURCE_RE = r"^order:[A-Z0-9][A-Z0-9-]*(/[a-z])?$"
_SOURCE = re.compile(SOURCE_RE)

# "b8:59:9f:ac:1e:a8", "b8599fac1ea8", "b859.9fac.1ea8" (any case).
_MAC_FORMS = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}",
    r"[0-9a-f]{12}",
    r"(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}",
)]
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


class BadSource(ValueError):
    """source is not an order:* source (raised before any DB access)."""


class BadMac(ValueError):
    """mac is not one of the accepted MAC spellings."""


class BadValue(ValueError):
    """ipv4 / hostname / vid is malformed."""


class Conflict(Exception):
    """The MAC or IP belongs to someone else; nothing was written."""


def _check_source(source) -> str:
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise BadSource(f"not an order source: {source!r}")
    return source


def _mac_hex(mac) -> str:
    if isinstance(mac, str) and any(f.fullmatch(mac) for f in _MAC_FORMS):
        return re.sub(r"[:.]", "", mac).lower()
    raise BadMac(f"bad mac: {mac!r}")


def _check_values(ipv4, hostname, vid) -> str:
    if not isinstance(vid, int) or isinstance(vid, bool) or not 1 <= vid <= 4094:
        raise BadValue(f"bad vid: {vid!r}")
    try:
        ip = str(ipaddress.IPv4Address(ipv4))
    except (ipaddress.AddressValueError, ValueError, TypeError):
        raise BadValue(f"bad ipv4: {ipv4!r}") from None
    if not isinstance(hostname, str) or not _HOSTNAME.fullmatch(hostname):
        raise BadValue(f"bad hostname: {hostname!r}")
    return ip


# Every row for this MAC, with its owner ('' when untagged).
_OWNER_SQL = """
SELECT COALESCE((user_context::jsonb) ->> 'source', '') AS owner_source
  FROM kea.hosts
 WHERE dhcp_identifier = decode(%(mac_hex)s, 'hex')
   AND dhcp_identifier_type = 0
"""

# Any other identifier holding this IP in this subnet.
_IP_HOLDER_SQL = """
SELECT encode(dhcp_identifier, 'hex') AS holder_mac
  FROM kea.hosts
 WHERE dhcp4_subnet_id = %(vid)s
   AND ipv4_address = (%(ipv4)s::inet - inet '0.0.0.0')::bigint
   AND NOT (dhcp_identifier = decode(%(mac_hex)s, 'hex')
            AND dhcp_identifier_type = 0)
"""

_UPSERT_SQL = """
INSERT INTO kea.hosts
    (dhcp_identifier, dhcp_identifier_type, ipv4_address, hostname,
     dhcp4_subnet_id, user_context)
VALUES
    (decode(%(mac_hex)s, 'hex'),
     0,                                                       -- 0 = hwaddr
     (%(ipv4)s::inet - inet '0.0.0.0')::bigint,
     %(hostname)s,
     %(vid)s,
     %(user_context)s)
-- Kea's unique constraint is a partial index (WHERE dhcp4_subnet_id IS NOT
-- NULL); ON CONFLICT needs the matching WHERE clause to use it.
ON CONFLICT (dhcp_identifier, dhcp_identifier_type, dhcp4_subnet_id)
  WHERE dhcp4_subnet_id IS NOT NULL
  DO UPDATE SET
    ipv4_address = EXCLUDED.ipv4_address,
    hostname     = EXCLUDED.hostname,
    -- merge, so an operator_note / aliases on our own row survives
    user_context = ((kea.hosts.user_context::jsonb)
                    || (EXCLUDED.user_context::jsonb))::text
  WHERE (kea.hosts.user_context::jsonb) ->> 'source' = %(source)s
"""


def upsert(*, source: str, mac: str, ipv4: str, hostname: str, vid: int) -> None:
    """Create or update the order reservation for `mac` in subnet `vid`.
    Raises BadSource/BadMac/BadValue before any DB access, Conflict when the
    MAC or IP is someone else's (the transaction rolls back, nothing written)."""
    _check_source(source)
    mac_hex = _mac_hex(mac)
    ip = _check_values(ipv4, hostname, vid)
    args = {"mac_hex": mac_hex, "ipv4": ip, "hostname": hostname, "vid": vid,
            "source": source, "user_context": json.dumps({"source": source})}
    with get_pool().connection() as conn:
        owners = sorted({r[0] for r in conn.execute(_OWNER_SQL, args).fetchall()}
                        - {source})
        if owners:
            raise Conflict(f"mac {mac_hex} already reserved by source "
                           + ", ".join(repr(o) for o in owners))
        holders = sorted({r[0] for r in conn.execute(_IP_HOLDER_SQL, args).fetchall()})
        if holders:
            raise Conflict(f"ip {ip} in subnet {vid} already held by "
                           + ", ".join(holders))
        if conn.execute(_UPSERT_SQL, args).rowcount != 1:
            raise Conflict(f"mac {mac_hex} subnet {vid} row changed owner "
                           "during upsert; nothing written")


def delete_source(source: str) -> int:
    """Delete every MAC reservation tagged exactly `source`. Returns the count
    (0 when already gone -- idempotent). v6 rows, if any, cascade."""
    _check_source(source)
    with get_pool().connection() as conn:
        return conn.execute(
            "DELETE FROM kea.hosts "
            " WHERE dhcp_identifier_type = 0 "
            "   AND (user_context::jsonb) ->> 'source' = %s",
            (source,)).rowcount


def list_source(source: str) -> list[dict]:
    """The reservations tagged exactly `source`: [{mac, ipv4, hostname, vid}]."""
    _check_source(source)
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT encode(dhcp_identifier, 'hex'), "
            "       host('0.0.0.0'::inet + ipv4_address) AS ipv4, "
            "       hostname, dhcp4_subnet_id "
            "  FROM kea.hosts "
            " WHERE dhcp_identifier_type = 0 "
            "   AND (user_context::jsonb) ->> 'source' = %s "
            " ORDER BY dhcp4_subnet_id, ipv4_address",
            (source,)).fetchall()
    return [{"mac": m, "ipv4": ip, "hostname": h, "vid": v} for m, ip, h, v in rows]
