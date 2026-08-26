# flax_post/state.py
"""Two-tier Postgres store for the post UI (docs/Post-UI-Design.md §3.3).

TIER 1 post_state — live, keyed by PORT (current slot occupant).
TIER 2 post_node  — durable node history, keyed by IDENTITY (bmc_mac->serial).
post_settings     — singleton operator context (order_no, population).

The post engine writes post_state + post_node; the viewer writes only
post_settings. vars_fields are merged into the row's JSONB `vars` so producers
contribute slices without clobbering each other. Generation is bumped on write.
"""
import json

from .db import get_pool


def read_state() -> dict:
    """{port: {port, switch, bmc_mac, serial, order_no, **vars}} for all live slots."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT port, switch, bmc_mac, serial, order_no, vars FROM post_state"
        ).fetchall()
    out = {}
    for port, switch, bmc_mac, serial, order_no, vars_ in rows:
        rec = {"port": port, "switch": switch, "bmc_mac": bmc_mac,
               "serial": serial, "order_no": order_no}
        if isinstance(vars_, dict):
            rec.update(vars_)
        out[port] = rec
    return out


def set_state(port, *, switch=None, bmc_mac=None, serial=None, order_no=None, **vars_fields) -> None:
    """Upsert post_state[port]; merge vars_fields into vars JSONB; bump generation."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO post_state (port, switch, bmc_mac, serial, order_no, vars) "
            "VALUES (%s, COALESCE(%s,'rabbit-edam'), %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (port) DO UPDATE SET "
            "  switch = COALESCE(EXCLUDED.switch, post_state.switch), "
            "  bmc_mac = COALESCE(EXCLUDED.bmc_mac, post_state.bmc_mac), "
            "  serial = COALESCE(EXCLUDED.serial, post_state.serial), "
            "  order_no = COALESCE(EXCLUDED.order_no, post_state.order_no), "
            "  vars = post_state.vars || EXCLUDED.vars, "
            "  generation = post_state.generation + 1, updated_at = NOW()",
            (port, switch, bmc_mac, serial, order_no, json.dumps(vars_fields)),
        )


def delete_state(port) -> None:
    """Delete the live post_state row for a port (orphan GC). post_node is kept."""
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM post_state WHERE port = %s", (port,))


def upsert_node(bmc_mac, *, serial=None, host_mac=None, order_no=None,
                last_switch=None, last_port=None, **vars_fields) -> None:
    """Upsert durable post_node[bmc_mac]; merge vars_fields into vars JSONB."""
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO post_node (bmc_mac, serial, host_mac, order_no, last_switch, last_port, vars) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (bmc_mac) DO UPDATE SET "
            "  serial = COALESCE(EXCLUDED.serial, post_node.serial), "
            "  host_mac = COALESCE(EXCLUDED.host_mac, post_node.host_mac), "
            "  order_no = COALESCE(EXCLUDED.order_no, post_node.order_no), "
            "  last_switch = COALESCE(EXCLUDED.last_switch, post_node.last_switch), "
            "  last_port = COALESCE(EXCLUDED.last_port, post_node.last_port), "
            "  vars = post_node.vars || EXCLUDED.vars, "
            "  generation = post_node.generation + 1, updated_at = NOW()",
            (bmc_mac, serial, host_mac, order_no, last_switch, last_port, json.dumps(vars_fields)),
        )


def read_settings() -> dict:
    """The singleton operator context {order_no, population, customer}."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT order_no, population, customer FROM post_settings WHERE id = 1"
        ).fetchall()
    if not row:
        return {"order_no": None, "population": None, "customer": None}
    order_no, population, customer = row[0]
    return {"order_no": order_no, "population": population, "customer": customer}


def write_settings(*, order_no=..., population=..., customer=...) -> None:
    """Update only the provided keys of the singleton (Ellipsis = leave unchanged)."""
    sets, params = [], []
    if order_no is not ...:
        sets.append("order_no = %s"); params.append(order_no)
    if population is not ...:
        sets.append("population = %s"); params.append(population)
    if customer is not ...:
        sets.append("customer = %s"); params.append(customer)
    if not sets:
        return
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE post_settings SET " + ", ".join(sets) + ", updated_at = NOW() WHERE id = 1",
            tuple(params),
        )


def write_artifact(bmc_mac, run_id, stage, name, kind, content, *,
                   serial=None, order_no=None, nbytes=None) -> None:
    """Upsert one durable evidence artifact, keyed by (bmc_mac, run_id, stage, name)."""
    if nbytes is None and content is not None:
        nbytes = len(content)
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO post_artifact (bmc_mac, serial, order_no, run_id, stage, name, kind, content, bytes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (bmc_mac, run_id, stage, name) DO UPDATE SET "
            "  serial = COALESCE(EXCLUDED.serial, post_artifact.serial), "
            "  order_no = COALESCE(EXCLUDED.order_no, post_artifact.order_no), "
            "  kind = EXCLUDED.kind, content = EXCLUDED.content, bytes = EXCLUDED.bytes, "
            "  captured_at = NOW()",
            (bmc_mac, serial, order_no, run_id, stage, name, kind, content, nbytes),
        )


def list_artifacts(bmc_mac, run_id, stage=None) -> list:
    """[{stage, name, kind, bytes}] for a node's run (optionally one stage); no content."""
    sql = "SELECT stage, name, kind, bytes FROM post_artifact WHERE bmc_mac = %s AND run_id = %s"
    params = [bmc_mac, run_id]
    if stage is not None:
        sql += " AND stage = %s"; params.append(stage)
    sql += " ORDER BY stage, name"
    with get_pool().connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [{"stage": s, "name": n, "kind": k, "bytes": b} for s, n, k, b in rows]


def get_artifact(bmc_mac, run_id, stage, name) -> "str | None":
    """One artifact's content, or None if absent."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT content FROM post_artifact "
            "WHERE bmc_mac = %s AND run_id = %s AND stage = %s AND name = %s",
            (bmc_mac, run_id, stage, name),
        ).fetchall()
    return rows[0][0] if rows else None


def purge_run(bmc_mac, run_id) -> None:
    """Delete all evidence rows for one abandoned qualification run (re-run purge)."""
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM post_artifact WHERE bmc_mac = %s AND run_id = %s",
            (bmc_mac, run_id),
        )
