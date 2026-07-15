"""Post-phase reservation engine: read switch_facts(<post switch>), derive
reservable targets with vid = live access_vid, compute deterministic IPs via
flax_classify.formula, name them with the rack prefix (rr003-et6b1) to avoid
the triage hostname collision, and upsert desired_reservations marked
owner_role='post'.

Post-3b demolition: this module writes desired_reservations ONLY. It no
longer touches kea.hosts / kea.ipv6_reservations directly -- the
materializer (flax_classify.materializer.run_cycle) is now the sole post
kea writer, reading this module's desired_reservations rows and reconciling
them against kea's actuals on its own pass.

Reuses formula.alloc_ip/alloc_ip6 so post and classify share one address
scheme.

``--from-node-macs FILE`` (default mode is switch_facts) restores the operator
override seam: the (editable) node_macs.txt is the authoritative bmc-per-port
source, so a hand-fixed BMC mac wins over the macmath heuristic; host NIC(s)
then come from each port's raw observed FDB macs.
"""
import argparse
import json
import logging
import sys

from .db import build_pool, read_switch_facts
from .desired_reservations import upsert_desired, delete_desired_slot
from .formula import alloc_ip, alloc_ip6, _normalise_rabbit_port

log = logging.getLogger(__name__)

_DEFAULT_SKIP = frozenset({"Ethernet1/1", "Management1"})


def _lldp_mac_norm(m):
    return (m or "").replace(":", "").replace("-", "").replace(".", "").lower()


def order_prefix(order_no: str) -> str:
    """Post-UI-Design 5.6 hostname order-prefix: lower(first-two-alpha +
    last-three-digit) of the order. 'RRORD-003' -> 'rr003'. Empty/None -> ''
    (the caller treats '' as 'no order' and writes nothing)."""
    if not order_no:
        return ""
    letters = "".join(c for c in order_no if c.isalpha())[:2]
    digits = "".join(c for c in order_no if c.isdigit())[-3:]
    return (letters + digits).lower()


_COMMS_CONFIRMED = frozenset({"probe_confirmed", "probe_promote_bmc"})


def derive_post_targets(switch_facts, switch, *, skip_ports=_DEFAULT_SKIP,
                        require_confirmed_bmc=False, observed=None,
                        allow_tokens=None):
    facts = switch_facts.get(switch)
    if not facts:
        return []
    observed = observed or {}
    out = []
    for port, info in (facts.get("ports") or {}).items():
        if port in skip_ports:
            continue
        if info.get("link") != "link" or info.get("mask") != "access":
            continue
        vid = info.get("access_vid")
        if vid is None:
            continue
        try:
            p, s = _normalise_rabbit_port(port)
        except ValueError:
            continue
        token = f"et{p}b{s}"
        if allow_tokens is not None and token not in allow_tokens:
            # Shared-switch scoping: only ports declared as post slots in
            # post-geometry.json (the run lane's per-switch allowlist) are post
            # targets — a co-located triage/other-role port on the same switch
            # is NOT, even if it is access + has a live BMC. On a dedicated post
            # switch allow_tokens covers every slot, so this is a no-op there.
            continue
        obs = observed.get((switch, token))
        src = obs.get("source") if obs else None
        if src in _COMMS_CONFIRMED and obs.get("bmc_mac"):
            # observe positively confirmed a live BMC by COMMUNICATION (redfish/ipmi/
            # openbmc over the IPv4 reservation or the MAC-derived IPv6 link-local --
            # a device that answers whenever the blade has power). This is the sole
            # authority for a BMC reservation.
            bmc_mac = obs["bmc_mac"]
            nic_macs = [obs["nic_mac"]] if obs.get("nic_mac") else (info.get("nic_macs") or [])
        elif require_confirmed_bmc:
            # Continuous post lane: no positive comms-confirmation -> NO BMC reservation.
            # switch_facts arithmetic + LLDP port_description=="eth0" are NOT proof of a
            # BMC: a mis-named booted host (ifname reverted from pxedev to eth0 after a
            # NIC-FW mstfwreset) advertises eth0 too, and the single-MAC collapse then
            # mislabels the lone host MAC as the BMC. probe_flip_host is observe telling
            # us "reached it, it's a host, no BMC here". flip / heuristic / no-row -> skip.
            continue
        else:
            # Operator/CLI path (require_confirmed_bmc=False): unchanged switch_facts
            # behavior, no gate (node_macs override + manual reserve rely on this).
            bmc_mac = info.get("bmc_mac")
            nic_macs = info.get("nic_macs") or []
            if not bmc_mac:
                continue
        out.append({"switch": switch, "port": port, "port_token": token,
                    "kind": "bmc", "mac": bmc_mac, "vid": int(vid)})
        for nic in nic_macs:
            # Never emit the BMC mac as a host target: desired_reservations
            # upserts ON CONFLICT (mac), and resolve appends host after bmc, so
            # a host row for bmc_mac would clobber the BMC's own reservation
            # (kind->host, wrong hostname/ipv4). On a +3/ME+1 blade the probe
            # PROMOTES a mac that switch_facts still lists in nic_macs, so this
            # guard is load-bearing (mirrors derive_targets_from_node_macs).
            if _lldp_mac_norm(nic) == _lldp_mac_norm(bmc_mac):
                continue
            out.append({"switch": switch, "port": port, "port_token": token,
                        "kind": "host", "mac": nic, "vid": int(vid)})
    return out


def _norm_mac(s):
    h = "".join(c for c in s.lower() if c in "0123456789abcdef")
    if len(h) != 12:
        raise ValueError(f"bad MAC: {s!r}")
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def parse_node_macs(text):
    """Parse a node_macs.txt body into ``[(bmc_mac, port_token), ...]``.

    Each non-blank, non-``#`` line is ``<bmc-mac-hex> <port_token>`` -- the
    format render_node_macs emits and donum 33 tees to node_macs.txt. The BMC
    mac is operator-authoritative: it may name a QUIET BMC that never appeared
    in the switch FDB (the whole point of the editable file).
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        out.append((_norm_mac(parts[0]), parts[1]))
    return out


def derive_targets_from_node_macs(node_macs, switch_facts, switch):
    """Build reservable targets from an (edited) node_macs list.

    The BMC mac comes from ``node_macs`` (operator-authoritative); the host
    NIC(s) are the port's RAW observed FDB macs minus that BMC. Using the raw
    ``macs`` -- not the classified ``nic_macs`` -- is what makes the override
    correct when the macmath heuristic mis-collapsed a single-MAC port (quiet
    BMC -> lone host mac mislabelled BMC + a phantom nic): the phantom lives
    only in the classified split, so the real host falls out and the phantom
    is never reserved. Mirrors the legacy makereservations seam (node_macs.txt
    authoritative) now that 36 writes kea.hosts via this engine.
    """
    facts = switch_facts.get(switch) or {}
    ports = facts.get("ports") or {}
    token_to_port = {}
    for port in ports:
        try:
            p, s = _normalise_rabbit_port(port)
        except ValueError:
            continue
        token_to_port[f"et{p}b{s}"] = port

    out = []
    for bmc_mac, token in node_macs:
        port = token_to_port.get(token)
        if port is None:
            log.warning("node_macs port %s not in switch_facts(%s); skipping",
                        token, switch)
            continue
        info = ports[port]
        vid = info.get("access_vid")
        if vid is None:
            log.warning("node_macs port %s has no access_vid; skipping", token)
            continue
        vid = int(vid)
        out.append({"switch": switch, "port": port, "port_token": token,
                    "kind": "bmc", "mac": bmc_mac, "vid": vid})
        for m in (info.get("macs") or []):
            host = _norm_mac(m)
            if host == bmc_mac:
                continue
            out.append({"switch": switch, "port": port, "port_token": token,
                        "kind": "host", "mac": host, "vid": vid})
    return out


def post_hostname(prefix, port, kind):
    p, s = _normalise_rabbit_port(port)
    # post_hostname OWNS the '-' between the order-prefix(+rack-tag) and the port;
    # tolerate a caller-supplied trailing dash so legacy '--prefix rr003-' still works.
    base = f"{prefix.rstrip('-')}-et{p}b{s}"
    return f"{base}-bmc" if kind == "bmc" else base


def resolve(targets, prefix):
    out = []
    for t in targets:
        r = dict(t)
        r["ipv4"] = alloc_ip(t["switch"], t["port"], t["vid"], t["kind"])
        r["ipv6"] = alloc_ip6(t["switch"], t["port"], t["vid"], t["kind"])
        r["hostname"] = post_hostname(prefix, t["port"], t["kind"])
        out.append(r)
    return out


def read_post_racks(geometry_path) -> list:
    """[(switch, rack_tag)] from post-geometry.json's racks LIST. Each rack is a
    {"switch":..., "tag":..., "label":...} object (the raw file format; the viewer's
    parse_geometry folds it into a dict, but we read the raw file). Missing or
    unreadable file -> []. tag defaults to '' (the primary rack)."""
    try:
        with open(geometry_path) as f:
            geo = json.load(f)
    except (OSError, ValueError):
        return []
    return [(r["switch"], r.get("tag", ""))
            for r in (geo.get("racks") or []) if isinstance(r, dict) and r.get("switch")]


def read_post_slots(geometry_path) -> dict:
    """{switch: {port_token, ...}} from post-geometry.json's slots LIST — the
    declared post rack slots per switch. Used as run_post_reservations' per-switch
    allowlist so post derivation is scoped to real post slots (excludes triage /
    other-role ports co-located on a shared switch). Missing/unreadable -> {}."""
    try:
        with open(geometry_path) as f:
            geo = json.load(f)
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for slot in (geo.get("slots") or []):
        if not isinstance(slot, dict):
            continue
        sw, port = slot.get("switch"), slot.get("port")
        if sw and port:
            out.setdefault(sw, set()).add(port)
    return out


def _norm(mac):
    return mac.strip().lower()


def observed_by_port(observe_rows):
    """observe_state rows -> {(switch, et{p}b{s} token): resolved}. Ports that
    don't normalize (non-rabbit) are skipped."""
    out = {}
    for row in observe_rows:
        try:
            p, s = _normalise_rabbit_port(row["port"])
        except (ValueError, KeyError):
            continue
        out[(row["switch"], f"et{p}b{s}")] = row.get("resolved") or {}
    return out


def run_post_reservations(pool, *, order_no, racks, facts, observed=None,
                          slots=None) -> dict:
    """Continuous post reservation lane. For each (switch, rack_tag) in `racks`,
    derive bmc/host targets from `facts` for observe comms-confirmed BMC ports only, name
    them with the order prefix (+rack tag), and upsert desired_reservations
    (owner_role='post'), sticky-purging any other desired occupant of that
    exact slot. `order_no` falsy -> no writes (hard kill switch; there is no
    default order). Returns {'written', 'purged', 'derived_macs'}.

    Post-3b demolition: the LEGACY kea writers this function used to call
    (upsert_kea_host, purge_superseded_slot_hosts) are gone -- the
    materializer's apply layer is now the sole post kea writer. What
    remains below is exactly the desired_reservations write path that used
    to run as an unconditional dual-write alongside them.

    derived_macs (final-review fix): the frozenset of macs (normalised
    lowercase) this call successfully upserted into desired_reservations this
    cycle. reconcile_post_reservations' keep-set pass needs this to know which
    macs already have a FRESH derived-desired row from THIS pass, so it can
    skip re-upserting them from the (potentially stale) actual kea row --
    see reconcile_post_reservations' docstring for why the keep-set echo was
    clobbering this exact derivation otherwise. A mac is only added here when
    its upsert_desired call actually succeeds.
    """
    if not order_no:
        return {"written": 0, "purged": 0}
    written = purged = 0
    derived_macs = set()
    for switch, rack_tag in racks:
        prefix = order_prefix(order_no) + rack_tag
        # Shared-switch scoping: when post-geometry slots are known, restrict this
        # switch's post derivation to its declared slot tokens (excludes triage /
        # other-role ports co-located on the same switch, e.g. braintree rabbit-lorax).
        # slots=None (or switch absent) -> no allowlist -> whole-switch (legacy).
        allow_tokens = slots.get(switch) if slots else None
        targets = derive_post_targets(facts, switch, require_confirmed_bmc=True,
                                      observed=observed, allow_tokens=allow_tokens)
        for r in resolve(targets, prefix):
            # desired_reservations write: this claim's mac/kind/vid/switch/
            # port/hostname/ipv4/ipv6, owner_role="post". Guarded per-call-
            # site so a write failure (incl UndefinedTable pre-migration)
            # can never affect a later target in this same pass.
            try:
                upsert_desired(
                    pool, owner_role="post", mac=r["mac"], kind=r["kind"],
                    hostname=r["hostname"], ipv4=r["ipv4"], ipv6=r["ipv6"],
                    vid=r["vid"], switch=r["switch"], port=r["port_token"])
                derived_macs.add(_norm(r["mac"]))
            except Exception:
                log.exception("desired write (post) failed for mac=%s",
                             r["mac"])
            # Sticky-slot purge: evict any OTHER desired_reservations row at
            # this exact (switch, port, kind) slot -- a prior blade's
            # stranded identity, superseded by r["mac"] now claiming it.
            try:
                purged += delete_desired_slot(
                    pool, owner_role="post", switch=r["switch"],
                    port=r["port_token"], kind=r["kind"], keep_mac=r["mac"])
            except Exception:
                log.exception("desired slot purge (post) failed for mac=%s",
                             r["mac"])
            written += 1
    return {"written": written, "purged": purged,
            "derived_macs": frozenset(derived_macs)}


def render_isc(resolved):
    # Mirrors makereservations printf: two-space indent, %-10s padded name,
    # single-line host{} block, tab + #<port_token> trailer.
    lines = []
    for r in resolved:
        name = r["hostname"]
        lines.append(
            f"  host {name:<10} {{hardware ethernet {r['mac']}; "
            f"fixed-address {r['ipv4']};}}\t#{r['port_token']}")
    return "\n".join(lines) + ("\n" if lines else "")


def render_node_macs(targets):
    seen, lines = set(), []
    for t in targets:
        if t["kind"] != "bmc":
            continue
        machex = t["mac"].replace(":", "").replace("-", "").replace(".", "").lower()
        key = (machex, t["port_token"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{machex} {t['port_token']}")
    return "\n".join(lines) + ("\n" if lines else "")


def _pool_and_facts(conninfo=None):
    if conninfo is None:
        from .__main__ import _build_conninfo  # local import avoids circular at module level
        conninfo = _build_conninfo()
    pool = build_pool(conninfo)
    return pool, read_switch_facts(pool)


def main(argv=None):
    """CLI entry point (donum-invoked). Post-3b demolition: writes
    desired_reservations ONLY (owner_role='post') -- the materializer
    (flax_classify.materializer.run_cycle) materializes these rows into
    kea.hosts / kea.ipv6_reservations; this CLI no longer writes kea
    directly nor purges any kea-side superseded-port occupant itself."""
    ap = argparse.ArgumentParser(prog="flax-classify-post-reserve")
    ap.add_argument("--switch", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--emit", choices=["isc", "node-macs"], default="isc")
    ap.add_argument("--from-node-macs", metavar="FILE",
                    help="read an (edited) node_macs.txt as the authoritative "
                         "bmc-per-port source ('-' = stdin); host NIC(s) come "
                         "from the port's raw observed FDB macs. Restores the "
                         "operator override seam donum 33 writes / 36 consumes.")
    args = ap.parse_args(argv)

    pool, facts = _pool_and_facts()
    if args.from_node_macs:
        text = (sys.stdin.read() if args.from_node_macs == "-"
                else open(args.from_node_macs).read())
        targets = derive_targets_from_node_macs(
            parse_node_macs(text), facts, args.switch)
    else:
        targets = derive_post_targets(facts, args.switch)
    if not targets:
        print(f"no reservable targets on {args.switch}", file=sys.stderr)
        return 1
    resolved = resolve(targets, args.prefix)
    if not args.dry_run:
        for r in resolved:
            # desired_reservations write ONLY -- the materializer
            # (flax_classify.materializer.run_cycle) is the sole kea writer
            # now. Guarded per-call-site: never breaks the CLI reservation
            # path for a later target.
            try:
                upsert_desired(
                    pool, owner_role="post", mac=r["mac"], kind=r["kind"],
                    hostname=r["hostname"], ipv4=r["ipv4"], ipv6=r["ipv6"],
                    vid=r["vid"], switch=r["switch"], port=r["port_token"])
            except Exception:
                log.exception("desired write (post) failed for mac=%s",
                             r["mac"])
    out = render_node_macs(targets) if args.emit == "node-macs" \
        else render_isc(resolved)
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
