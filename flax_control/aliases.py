"""Read-merge-write helper for kea.hosts.user_context.aliases — extra DNS names.

The operator types a space-separated list ("alias1 alias2 alias3") in the
reservations page. We parse → validate (each must be a DNS-safe hostname/FQDN) →
dedup → store as a JSON list under user_context.aliases, preserving the other keys
(classify, operator_note). flax-classify reads this list each cycle and emits the
extra names alongside the device's primary hostname in the dnsmasq hosts file.

Mirrors flax_control/operator_notes (same identity + read-merge-write + TEXT
user_context). aliases survive each classify cycle for the same reason
operator_note does: the upsert's `user_context || jsonb_build_object('classify',…)`
merge preserves unknown keys.
"""
import json
import re

from .db import get_pool

_MAC_HEX_RE = re.compile(r"^[0-9a-fA-F]{12}$")
# A single DNS label or dotted FQDN (RFC 1123): labels of [a-z0-9-], no leading/
# trailing hyphen, no empty labels, total <= 253. Input is lowercased first.
_ALIAS_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")


class NotFound(Exception):
    """mac_hex is not a valid 12-char MAC, or the reservation doesn't exist."""


class InvalidAlias(ValueError):
    """A token in the alias list is not a DNS-safe hostname."""


def parse_aliases(text: str) -> list:
    """Split on whitespace, lowercase, validate each token, dedup (order-stable).
    Empty/whitespace input -> []. Raises InvalidAlias on the first bad token."""
    out = []
    for tok in (text or "").split():
        name = tok.lower()
        if not _ALIAS_RE.match(name):
            raise InvalidAlias(tok)
        if name not in out:
            out.append(name)
    return out


def update_aliases(mac_hex: str, aliases_text: str) -> list:
    """Set user_context.aliases from the space-separated text (empty clears it).
    Returns the parsed list. Raises NotFound (bad/missing mac) or InvalidAlias."""
    if not _MAC_HEX_RE.match(mac_hex):
        raise NotFound(mac_hex)
    parsed = parse_aliases(aliases_text)  # validate BEFORE touching the DB
    with get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT user_context FROM kea.hosts "
            "WHERE dhcp_identifier = decode(%s, 'hex') "
            "  AND dhcp_identifier_type = 0",
            (mac_hex,))
        row = cur.fetchone()
        if row is None:
            raise NotFound(mac_hex)
        raw = row[0]
        ctx = json.loads(raw) if raw else {}
        if parsed:
            ctx["aliases"] = parsed
        else:
            ctx.pop("aliases", None)
        conn.execute(
            "UPDATE kea.hosts SET user_context = %s "
            "WHERE dhcp_identifier = decode(%s, 'hex') "
            "  AND dhcp_identifier_type = 0",
            (json.dumps(ctx), mac_hex))
    return parsed
