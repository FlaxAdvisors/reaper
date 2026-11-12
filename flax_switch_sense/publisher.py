"""Build switch_facts rows + write them to Postgres."""
import datetime

from .classify import classify_macs


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _macmath_for_port(p: dict, macmath_by_vid: dict) -> dict | None:
    """Resolve the per-vid macmath config for one port, or None.

    The port's ``access_vid`` (set by slice_by_port for access ports only;
    omitted on trunks / unknown) selects the config. We coerce to int since
    parsed vids may arrive as str. A missing/unknown/unparseable vid -> None,
    which restores the legacy ±2/4/6/8 pairing in classify_macs.
    """
    if not macmath_by_vid:
        return None
    raw_vid = p.get("access_vid")
    if raw_vid is None:
        return None
    try:
        vid = int(raw_vid)
    except (TypeError, ValueError):
        return None
    return macmath_by_vid.get(vid)


def build_switch_facts_row(switch: str, driver: str,
                            per_port: dict[str, dict],
                            *, reachable: bool,
                            macmath_by_vid: dict | None = None) -> dict:
    """Apply classify_macs to every port and assemble the JSONB payload
    for the switch_facts row.

    ``macmath_by_vid`` is the ``{vid: config}`` mapping from
    ``macmath.load_macmath_dir``. Each port's ``access_vid`` selects its
    config; a port with no config for its vid (or no access_vid) is
    classified with the legacy ±2/4/6/8 pairing. Defaults to ``{}`` so
    callers that don't supply it get unchanged legacy behavior.

    Generation is NOT set here -- the UPSERT in write_switch_facts increments
    it atomically against the current row's value.
    """
    if macmath_by_vid is None:
        macmath_by_vid = {}
    port_mask: dict[str, str] = {}
    enriched: dict[str, dict] = {}
    for port, p in per_port.items():
        if "mask" in p:
            port_mask[port] = p["mask"]
        macmath = _macmath_for_port(p, macmath_by_vid)
        c = classify_macs(p.get("macs", []), p.get("lldp_neighbors", []),
                          macmath=macmath)
        enriched[port] = {
            **p,
            "bmc_mac": c.bmc,
            "nic_macs": list(c.nics),
            "junk_macs": list(c.junk),
            "classification_source": c.classification_source,
            "lldp_disagreement": c.lldp_disagreement,
        }
    return {
        "switch": switch,
        "driver": driver,
        "polled_at": _now_iso(),
        "reachable": reachable,
        "port_mask": port_mask,
        "ports": enriched,
    }


import json

from .db import get_pool


def write_switch_facts(row: dict) -> int:
    """UPSERT one switch_facts row, atomically incrementing generation.
    Returns the new generation value (useful for tests + ack tracking)."""
    sql = """
        INSERT INTO switch_facts
          (switch, driver, polled_at, reachable, generation, port_mask, ports)
        VALUES
          (%(switch)s, %(driver)s, %(polled_at)s, %(reachable)s, 1,
           %(port_mask)s, %(ports)s)
        ON CONFLICT (switch) DO UPDATE SET
          driver     = EXCLUDED.driver,
          polled_at  = EXCLUDED.polled_at,
          reachable  = EXCLUDED.reachable,
          port_mask  = EXCLUDED.port_mask,
          ports      = EXCLUDED.ports,
          generation = switch_facts.generation + 1
        RETURNING generation
    """
    params = {
        "switch":     row["switch"],
        "driver":     row["driver"],
        "polled_at":  row["polled_at"],
        "reachable":  row["reachable"],
        "port_mask":  json.dumps(row["port_mask"]),
        "ports":      json.dumps(row["ports"]),
    }
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            (gen,) = cur.fetchone()
    return gen


def write_ack(pool, consumer, source, generation, action, detail=None):
    """Upsert the consumer_acks high-water-mark for (consumer, source)."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    sql = (
        "INSERT INTO consumer_acks "
        "(consumer, source, generation, action, consumed_at, detail) "
        "VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (consumer, source) DO UPDATE SET "
        "generation=GREATEST(consumer_acks.generation, EXCLUDED.generation), "
        "action=EXCLUDED.action, consumed_at=EXCLUDED.consumed_at, detail=EXCLUDED.detail"
    )
    with pool.connection() as conn:
        conn.execute(sql, (consumer, source, int(generation), action, now, detail))
