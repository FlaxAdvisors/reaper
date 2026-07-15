"""Postgres I/O for flax-classify's kea.hosts upsert (Plan 5).

Replaces the Plan 4 classify_proposals writes. Three semantic differences
from the shadow-table writer:

  1. hwaddr is BYTEA (not TEXT) -- mac string is decoded server-side
     via Postgres's decode(...) function.

  2. ipv4_address is BIGINT (Kea convention -- host-order 32-bit int),
     not INET. Convert the "172.17.6.101" classify_one string via
     (inet '...' - inet '0.0.0.0')::bigint so Postgres builds the
     numeric form server-side.

  3. The classify metadata that previously lived in dedicated columns
     (switch, port, kind, vid, generation, updated_at) now lives in
     user_context.classify as a JSON sub-object. ON CONFLICT uses
     jsonb_object || jsonb_build_object(...) so any other keys in
     user_context (notably operator_note, written by flax-control via
     PATCH) survive the upsert.

  4. The legacy-import tag (user_context.source == 'legacy-import', imported
     legacy reservations seeded during the Kea cutover) no longer confers
     sweep immunity here -- the global stale-sweep that once preserved it
     (delete_stale_kea_hosts) was retired in the phase-3b demolition (the
     apply layer is now the sole per-mac writer/deleter; see
     delete_other_subnet_rows and delete_hosts_for_mac below). What remains
     live is the upsert's ON CONFLICT merge, which still refuses to let a
     re-upsert's own source clobber an EXISTING row's legacy-import tag (see
     the CASE guard in _UPSERT_SQL) -- a narrower, upsert-time-only
     protection, not a sweep-time one.
"""
import logging

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


def _mac_hex(mac: str) -> str:
    """1c:34:da:7f:b3:a4 -> 1c34da7fb3a4 (lower)."""
    return mac.replace(":", "").replace("-", "").replace(".", "").lower()


_UPSERT_SQL = """
INSERT INTO kea.hosts
    (dhcp_identifier, dhcp_identifier_type, ipv4_address, hostname,
     dhcp4_subnet_id, dhcp6_subnet_id, user_context)
VALUES
    (decode(%(mac_hex)s, 'hex'),
     0,                                                       -- 0 = hwaddr
     (%(ipv4_address)s::inet - inet '0.0.0.0')::bigint,
     %(hostname)s,
     %(dhcp4_subnet_id)s,
     %(dhcp6_subnet_id)s,
     (jsonb_build_object('classify', %(classify_meta)s::jsonb)
      || %(extra_ctx)s::jsonb)::text)
-- Kea's unique constraint is a partial index (WHERE dhcp4_subnet_id IS NOT
-- NULL); ON CONFLICT needs the matching WHERE clause to use it.
ON CONFLICT (dhcp_identifier, dhcp_identifier_type, dhcp4_subnet_id)
  WHERE dhcp4_subnet_id IS NOT NULL
  DO UPDATE SET
    ipv4_address = EXCLUDED.ipv4_address,
    hostname     = EXCLUDED.hostname,
    dhcp6_subnet_id = EXCLUDED.dhcp6_subnet_id,
    -- Legacy-import tag protection: if the EXISTING row is tagged
    -- source='legacy-import' (a Kea-cutover seed; see the module docstring's
    -- point 4 for why sweep-time immunity is no longer the mechanism here),
    -- a re-upsert carrying
    -- extra_ctx={"source": "triage"/"post"/...} must NOT overwrite that tag
    -- -- doing so would silently strip the row's sweep immunity, breaching
    -- the "byte-identical except source=triage" contract for legacy rows.
    -- Strip extra_ctx's own 'source' key before merging whenever the
    -- EXISTING row is legacy-import; every other key in extra_ctx (there
    -- are currently none besides 'source') still merges normally.
    user_context = (
        COALESCE(kea.hosts.user_context, '{}')::jsonb
        || jsonb_build_object('classify', %(classify_meta)s::jsonb)
        || (CASE
              WHEN COALESCE((kea.hosts.user_context::jsonb) ->> 'source', '')
                   = 'legacy-import'
                THEN (%(extra_ctx)s::jsonb) - 'source'
              ELSE %(extra_ctx)s::jsonb
            END)
    )::text
RETURNING host_id
"""

# v6 reservation rewrite for a host: clear any prior rows then mint the one
# reservation mirroring the v4 host octets. FK is ON DELETE CASCADE, so a
# kea.hosts sweep already drops these -- no change to the sweep functions.
_DELETE_V6_SQL = "DELETE FROM kea.ipv6_reservations WHERE host_id = %s"
_INSERT_V6_SQL = (
    "INSERT INTO kea.ipv6_reservations (address, prefix_len, type, host_id) "
    "VALUES (%s::inet, 128, 0, %s)"
)


def upsert_kea_host(pool: ConnectionPool, *,
                    switch: str, port: str, mac: str, kind: str,
                    vid: int, ipv4_address: str, hostname: str,
                    ipv6_address: str, source: str | None = None) -> None:
    """Idempotent upsert. Preserves user_context.operator_note via ||
    JSON concatenation on conflict. Writes the host's IPv6 reservation in the
    SAME transaction (atomic per device): RETURNING host_id from the upsert,
    then rewrite kea.ipv6_reservations (DELETE existing rows for the host_id,
    INSERT the v6 address)."""
    classify_meta = Jsonb({
        "switch": switch, "port": port, "kind": kind, "vid": vid,
    })
    extra_ctx = Jsonb({"source": source} if source else {})
    with pool.connection() as conn:
        cur = conn.execute(_UPSERT_SQL, {
            "mac_hex": _mac_hex(mac),
            "ipv4_address": ipv4_address,
            "hostname": hostname,
            "dhcp4_subnet_id": vid,
            "dhcp6_subnet_id": vid,
            "classify_meta": classify_meta,
            "extra_ctx": extra_ctx,
        })
        host_id = cur.fetchone()[0]
        conn.execute(_DELETE_V6_SQL, (host_id,))
        conn.execute(_INSERT_V6_SQL, (ipv6_address, host_id))


# Project host(address): ipv6_reservations.address is INET (renders with a
# /128 suffix), but kea.lease6.address is VARCHAR holding the bare canonical
# address -- host() strips the prefix so the lease6 release actually matches.
_SELECT_V6_ADDRS = "SELECT host(address) FROM kea.ipv6_reservations WHERE host_id = ANY(%s)"


def release_leases_for_hosts(pool: ConnectionPool, *, hwaddrs, v6_addrs):
    """Best-effort release of leases for swept reservations via the
    kea.flax_classify_release_leases SECURITY DEFINER function (migration 023):
    flax_classify lacks direct DELETE on kea.lease4/lease6, and the function
    runs as its owner with search_path=kea so Kea's lease triggers resolve.
    The CALLER wraps this in try/except: a release failure must not fail the
    cycle. Deletes kea.lease4 by hwaddr (bytea) and kea.lease6 by reserved
    address (text).
    """
    if not hwaddrs and not v6_addrs:
        return
    with pool.connection() as conn:
        conn.execute(
            "SELECT kea.flax_classify_release_leases(%s::bytea[], %s::text[])",
            (list(hwaddrs), list(v6_addrs)),
        )


# Post-3b demolition removed four dead kea.hosts sweep/purge functions that
# had no remaining callers (the materializer's apply layer -- delete_hosts_for_mac
# / delete_other_subnet_rows below -- is now the sole writer/deleter, per-mac
# and per-source scoped, not a global sweep):
#   - delete_stale_kea_hosts (+ its _STALE_WHERE_* keep-set guards, which
#     excluded both the legacy-import and post sources from the global sweep)
#     -- the global keep-set sweep.
#   - purge_superseded_port_hosts -- port-scoped eviction of any other
#     reservation at a (switch, port) once a live occupant appears, including
#     legacy-import rows. Its rationale is carried forward in
#     delete_other_subnet_rows' docstring below (this task's binding
#     rationale-carry-forward rule).
#   - purge_superseded_slot_hosts -- sticky per-(switch, port, kind) eviction;
#     superseded by delete_desired_slot's desired_reservations-side sweep
#     (post_reserve.py) plus the materializer's own delete action.
#   - purge_relocated_mac_hosts -- per-mac cross-subnet relocation cleanup;
#     its rationale was already carried into delete_other_subnet_rows' docstring
#     in Task 2 (the phase-3a apply-layer generalization of this same cleanup).
# See tests/test_flax_classify_kea_hosts_repo_shape.py for the pinned-survivor
# gate that fails loudly if a sweep-shaped exclusion literal is ever
# reintroduced into this module.


# Canonical colon-lowercase mac from the dhcp_identifier bytea. Functionally
# equivalent to (renders the same colon-lowercase MAC as) flax_post.queries.
# _MAC_SQL, but that template is parameterized with %s for the column name --
# here the column (dhcp_identifier) is inlined, so it is NOT the same SQL
# mechanism, just the same output shape.
_POST_MAC_SQL = ("regexp_replace(encode(dhcp_identifier, 'hex'), "
                 r"'(..)(..)(..)(..)(..)(..)', E'\\1:\\2:\\3:\\4:\\5:\\6')")

# Keep-set fields (Task 2, phase 3a): kind/hostname/vid/ipv4/ipv6 added
# alongside the original mac/switch/port/post/operator_note so the post lane
# can carry the same comparison fields the materializer's planner uses
# (_COMPARE_FIELDS in materializer.py) without a second reader. ipv4 and ipv6
# mirror _READ_ACTUALS_SQL exactly: host() wraps the inet+bigint sum (a bare
# ::text cast would leave the /32 suffix on -- see the regression test for
# _READ_ACTUALS_SQL, bang-gouda 2026-07-04), and ipv6 comes from the same
# LEFT JOIN kea.ipv6_reservations shape. NULL-safety: Postgres's built-in
# inet '+' operator and host() are both STRICT (NULL in -> NULL out), so
# '0.0.0.0'::inet + NULL propagates to NULL and host(NULL) is NULL -- no CASE
# guard needed (upstream Kea's ipv4_address is NOT NULL DEFAULT 0 in practice
# anyway, so this only matters defensively). Row multiplication from the v6
# join: like _READ_ACTUALS_SQL, this relies on the invariant that
# upsert_kea_host's DELETE-then-INSERT keeps at most one ipv6_reservations
# row per host_id -- no DISTINCT/dedup needed here either.
_READ_POST_SQL = (
    "SELECT " + _POST_MAC_SQL + " AS mac, "
    "(user_context::jsonb)->'classify'->>'switch' AS switch, "
    "(user_context::jsonb)->'classify'->>'port'   AS port, "
    "(user_context::jsonb)->'classify'->>'kind'   AS kind, "
    "h.hostname AS hostname, "
    "h.dhcp4_subnet_id AS vid, "
    "host('0.0.0.0'::inet + h.ipv4_address) AS ipv4, "
    "host(v6.address) AS ipv6, "
    "COALESCE((user_context::jsonb)->'post', '{}'::jsonb) AS post, "
    "COALESCE((user_context::jsonb)->>'operator_note','') <> '' AS operator_note "
    "FROM kea.hosts h "
    "LEFT JOIN kea.ipv6_reservations v6 ON v6.host_id = h.host_id "
    "WHERE dhcp_identifier_type = 0 "
    "  AND COALESCE((user_context::jsonb)->>'source','') = 'post'"
)


def read_post_reservations(pool: ConnectionPool) -> list[dict]:
    """All source='post' reservations with their lifecycle timers.

    Each row: {mac (colon-lower), switch, port (internal token), kind, hostname,
    vid (int, dhcp4_subnet_id), ipv4 (dotted text or None), ipv6 (text or None),
    post (dict from user_context.post), operator_note (bool)}.
    """
    with pool.connection() as conn:
        rows = conn.execute(_READ_POST_SQL).fetchall()
    return [{"mac": m, "switch": sw, "port": pt, "kind": kind, "hostname": hostname,
             "vid": vid, "ipv4": ipv4, "ipv6": ipv6, "post": (po or {}),
             "operator_note": bool(note)}
            for (m, sw, pt, kind, hostname, vid, ipv4, ipv6, po, note) in rows]


# mac-scoped, source-scoped delete (preserving operator_note) -- the phase-3a
# apply layer's generalization of the retired delete_post_hosts' shape over an
# arbitrary `source` (any registered owner role, not just 'post'). Used for
# both the plain "delete" action (mac's owner has no desired row) and the
# "purge_handoff" action's old-owner-eviction step (mac changed owners).
# NOTE: this filters on user_context.source = %(source)s exactly -- it does
# NOT match the legacy untagged-triage transition shape (source IS NULL,
# has_classify true) that plan_materialization's owner attribution treats as
# "triage". A STALE untagged row (device gone, so no cycle ever re-upserts/
# re-tags it) is NOT self-healing; the global delete_stale_kea_hosts sweep
# that used to cover this for the whole shadow period was retired in the
# phase-3b demolition (Task 3), so post-3b the sole backstop is the pre-flip
# checkpoint gate requiring zero untagged-with-classify rows before triage may
# be armed. Post-flip, no new untagged rows can originate in-system (every
# triage write carries source="triage"). A row that somehow slipped past that
# gate would survive a planned delete_hosts_for_mac(source="triage") call,
# recorded as deleted_rows=0 in its plan row (the observable residue) -- a
# known, accepted limitation, not a bug in this helper.
_DELETE_MAC_WHERE = """
 WHERE dhcp_identifier = %(mac)s
   AND dhcp_identifier_type = 0
   AND COALESCE((user_context::jsonb) ->> 'operator_note', '') = ''
   AND COALESCE((user_context::jsonb) ->> 'source', '') = %(source)s
"""
_SELECT_DELETE_MAC_SQL = "SELECT host_id, dhcp_identifier FROM kea.hosts" + _DELETE_MAC_WHERE
_DELETE_MAC_SQL = "DELETE FROM kea.hosts" + _DELETE_MAC_WHERE


def delete_hosts_for_mac(pool: ConnectionPool, *, mac: str, source: str) -> int:
    """Delete every kea.hosts row for `mac` tagged user_context.source ==
    `source`, preserving operator_note rows, then best-effort release its
    v4+v6 leases. Returns the deleted row count. See _DELETE_MAC_WHERE above
    for the untagged-legacy-row caveat.
    """
    args = {"mac": bytes.fromhex(_mac_hex(mac)), "source": source}
    with pool.connection() as conn:
        rows = conn.execute(_SELECT_DELETE_MAC_SQL, args).fetchall()
        host_ids = [r[0] for r in rows]
        hwaddrs = [r[1] for r in rows]
        v6_addrs = []
        if host_ids:
            v6_addrs = [r[0] for r in
                        conn.execute(_SELECT_V6_ADDRS, (host_ids,)).fetchall()]
        deleted = conn.execute(_DELETE_MAC_SQL, args).rowcount
    try:
        release_leases_for_hosts(pool, hwaddrs=hwaddrs, v6_addrs=v6_addrs)
    except Exception as e:
        log.warning("lease release after mac-scoped delete failed (mac=%s, "
                    "source=%s, count=%d): %s", mac, source, deleted, e)
    return deleted


# Apply-time relocation cleanup (post-3b): the SAME source's rows for a mac
# in any OTHER subnet than the one just upserted. operator_note rows are
# excluded IN SQL (deliberate human override survives; if one dups the mac
# across subnets, the planner's multi_actual skip is the correct visible
# outcome next cycle, not a silent delete here).
_DELETE_OTHER_SUBNET_WHERE = """
 WHERE dhcp_identifier = %(mac)s
   AND dhcp_identifier_type = 0
   AND dhcp4_subnet_id <> %(keep_vid)s
   AND COALESCE((user_context::jsonb) ->> 'operator_note', '') = ''
   AND COALESCE((user_context::jsonb) ->> 'source', '') = %(source)s
"""
_SELECT_OTHER_SUBNET_SQL = ("SELECT host_id, dhcp_identifier FROM kea.hosts"
                            + _DELETE_OTHER_SUBNET_WHERE)
_DELETE_OTHER_SUBNET_SQL = "DELETE FROM kea.hosts" + _DELETE_OTHER_SUBNET_WHERE


def delete_other_subnet_rows(pool: ConnectionPool, *, mac: str, source: str,
                             keep_vid: int) -> int:
    """Delete `source`'s kea.hosts rows for `mac` in any subnet OTHER than
    `keep_vid`, preserving operator_note rows, then best-effort release the
    doomed rows' v4+v6 leases (delete_hosts_for_mac's select->delete->release
    shape). Returns the deleted row count.

    Why this exists (carried forward from the retired
    purge_relocated_mac_hosts): upsert_kea_host's conflict key is
    (dhcp_identifier, dhcp_identifier_type, dhcp4_subnet_id), so a
    same-subnet move updates the row in place, but a subnet/vid-CHANGING
    move INSERTs a fresh new-subnet row and leaves the old-subnet row
    behind -- the dup only arises when the move changes the subnet/vid.
    Post-3b the materializer's apply layer is the sole kea writer, so it
    must do this cleanup itself right after a vid-moving upsert; otherwise
    the stranded old-subnet row makes the mac two actuals next cycle,
    tripping the planner's multi_actual skip and freezing the mac forever
    (the skip precedes the delete branch, so even a later desired-row
    removal never plans).

    Unlike purge_relocated_mac_hosts this delete is SOURCE-scoped -- it
    never touches another owner's (or an untagged/legacy) row for the mac;
    those genuinely ambiguous states stay with the planner's multi_actual
    policy. The legacy purge also historically evicted legacy-import seed
    rows on relocation; that duty is moot on the live deploy -- gouda has
    ZERO legacy-import rows (verified 2026-07-05) and the deploy gate
    asserts the count stays zero.

    A second retired function's rationale is carried forward here too
    (Task 3, phase-3b): purge_superseded_port_hosts evicted EVERY OTHER
    reservation at a (switch, port) -- INCLUDING legacy-import seeds --
    the instant a live occupant reappeared there, on the "occupancy proof"
    principle: a live device physically present on a port is definitive
    evidence any other reservation claiming that same port is a stale prior
    occupant (e.g. a Kea-cutover seed superseded by a swapped-in chassis),
    so it must be evicted for the new occupant's DHCP to converge cleanly.
    Post-3b that duty is subsumed by this function's per-mac, per-source
    cleanup plus the materializer's own delete action -- there is no
    port-wide sweep anymore, only mac-scoped ones. Like the relocation
    purge's carried-forward duty above, evicting legacy-import rows on port
    reoccupation is moot on the live deploy (gouda: zero legacy-import rows,
    deploy gate asserts the count stays zero); if legacy-import seeding is
    ever reintroduced, this port-reoccupation eviction rationale must be
    revisited -- a stray legacy-import row occupying a port that a live
    device now needs would otherwise sit un-evicted.
    """
    args = {"mac": bytes.fromhex(_mac_hex(mac)), "source": source,
            "keep_vid": keep_vid}
    with pool.connection() as conn:
        rows = conn.execute(_SELECT_OTHER_SUBNET_SQL, args).fetchall()
        host_ids = [r[0] for r in rows]
        hwaddrs = [r[1] for r in rows]
        v6_addrs = []
        if host_ids:
            v6_addrs = [r[0] for r in
                        conn.execute(_SELECT_V6_ADDRS, (host_ids,)).fetchall()]
        deleted = conn.execute(_DELETE_OTHER_SUBNET_SQL, args).rowcount
    try:
        release_leases_for_hosts(pool, hwaddrs=hwaddrs, v6_addrs=v6_addrs)
    except Exception as e:
        log.warning("lease release after other-subnet cleanup failed (mac=%s, "
                    "source=%s, keep_vid=%s, count=%d): %s",
                    mac, source, keep_vid, deleted, e)
    return deleted


_STAMP_POST_SQL = (
    "UPDATE kea.hosts "
    "SET user_context = ((user_context::jsonb) || "
    "                    jsonb_build_object('post', %(post)s::jsonb))::text "
    "WHERE dhcp_identifier = %(mac)s AND dhcp_identifier_type = 0 "
    "  AND COALESCE((user_context::jsonb) ->> 'operator_note', '') = '' "
    "  AND COALESCE((user_context::jsonb) ->> 'source', '') = 'post'"
)


def stamp_post_timers(pool: ConnectionPool, timer_writes: dict) -> None:
    """Merge each mac's post sub-dict into user_context.post (source='post' only).
    timer_writes: {mac_str: post_dict}. An empty post_dict clears the timers.
    """
    if not timer_writes:
        return
    with pool.connection() as conn:
        for mac, post in timer_writes.items():
            conn.execute(_STAMP_POST_SQL,
                         {"post": Jsonb(post),
                          "mac": bytes.fromhex(_mac_hex(mac))})


# Shadow materializer (Task 5) READ-ONLY snapshot of kea.hosts for the diff
# engine: every field the planner compares against desired_reservations, plus
# the ownership-attribution inputs (source, has_classify). ipv4_address is
# BIGINT (Kea convention, see module docstring point 2) -- rendered back to
# dotted-quad text with the mirror-image arithmetic of the upsert's encode
# ("172.17.6.101"::inet - inet '0.0.0.0')::bigint. host() is REQUIRED around
# the inet+bigint sum: a bare ::text cast renders "172.17.7.4/32" (the /32
# survives), which made every converged row plan a perpetual ipv4-drift
# upsert on the live deploy (bang-gouda 2026-07-04). host() strips the mask,
# same as the ipv6 column below. NO writes -- SELECT only.
# operator_note added (phase 3a, Task 2): the apply layer must never delete
# an operator-curated actual row (mirrors the legacy sweep's protection --
# see _STALE_WHERE_* above); same expression _READ_POST_SQL uses. The
# planner (materializer.plan_materialization) ignores unknown actual-dict
# keys -- it only ever reads _COMPARE_FIELDS plus source/has_classify -- so
# this is a purely additive column for this shadow-only reader.
_READ_ACTUALS_SQL = (
    "SELECT " + _POST_MAC_SQL + " AS mac, "
    "host('0.0.0.0'::inet + h.ipv4_address) AS ipv4, "
    "h.hostname AS hostname, "
    "h.dhcp4_subnet_id AS vid, "
    "host(v6.address) AS ipv6, "
    "(h.user_context::jsonb) ->> 'source' AS source, "
    "(h.user_context::jsonb) ? 'classify' AS has_classify, "
    "COALESCE((h.user_context::jsonb)->>'operator_note','') <> '' AS operator_note "
    "FROM kea.hosts h "
    "LEFT JOIN kea.ipv6_reservations v6 ON v6.host_id = h.host_id "
    "WHERE h.dhcp_identifier_type = 0"
)


def read_kea_actuals(pool: ConnectionPool) -> list[dict]:
    """READ-ONLY snapshot of every type-0 kea.hosts row, for the shadow
    materializer's diff engine. Each row: {mac (colon-lower), ipv4 (dotted
    text or None), hostname, vid (dhcp4_subnet_id), ipv6 (text or None),
    source (user_context.source or None), has_classify (bool), operator_note
    (bool)}. This function issues a SELECT only -- it must never gain an
    INSERT/UPDATE/DELETE (shadow-only invariant, phase 2)."""
    with pool.connection() as conn:
        rows = conn.execute(_READ_ACTUALS_SQL).fetchall()
    return [
        {"mac": mac, "ipv4": ipv4, "hostname": hostname, "vid": vid,
         "ipv6": ipv6, "source": source, "has_classify": bool(has_classify),
         "operator_note": bool(operator_note)}
        for (mac, ipv4, hostname, vid, ipv6, source, has_classify, operator_note) in rows
    ]


_READ_ALIASES_SQL = """
SELECT encode(dhcp_identifier, 'hex') AS mh,
       user_context::jsonb -> 'aliases' AS al
  FROM kea.hosts
 WHERE dhcp_identifier_type = 0
   AND dhcp_identifier = ANY(%s)
   AND user_context::jsonb ? 'aliases'
"""


def read_aliases_for_macs(pool: ConnectionPool, macs) -> dict:
    """Return {mac: [aliases]} for the given MAC strings, reading
    user_context.aliases (a JSON array written by flax-control's WebUI) from the
    type-0 kea.hosts rows. MACs with no aliases are omitted. Result keys are in
    the same string form as the input macs. Used by the cycle to append operator
    aliases to the dnsmasq hosts file alongside each device's primary hostname.
    """
    macs = list(macs)
    if not macs:
        return {}
    by_hex = {_mac_hex(m): m for m in macs}
    keep_bytes = [bytes.fromhex(h) for h in by_hex]
    out = {}
    with pool.connection() as conn:
        cur = conn.execute(_READ_ALIASES_SQL, (keep_bytes,))
        for mh, al in cur.fetchall():
            if al:
                out[by_hex.get(mh, mh)] = list(al)
    return out
