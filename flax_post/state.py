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
    """The singleton operator context {order_no, population, customer, sales}."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT order_no, population, customer, sales, updated_at FROM post_settings WHERE id = 1"
        ).fetchall()
    if not row:
        return {"order_no": None, "population": None, "customer": None, "sales": None,
                "updated_at": None}
    order_no, population, customer, sales, updated_at = (tuple(row[0]) + (None,) * 5)[:5]
    if updated_at is not None and not isinstance(updated_at, str):
        updated_at = updated_at.isoformat()
    return {"order_no": order_no, "population": population, "customer": customer,
            "sales": sales, "updated_at": updated_at}


def write_settings(*, order_no=..., population=..., customer=..., sales=...) -> None:
    """Update only the provided keys of the singleton (Ellipsis = leave unchanged)."""
    sets, params = [], []
    for col, val in (("order_no", order_no), ("population", population),
                     ("customer", customer), ("sales", sales)):
        if val is not ...:
            sets.append(col + " = %s"); params.append(val)
    if not sets:
        return
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE post_settings SET " + ", ".join(sets) + ", updated_at = NOW() WHERE id = 1",
            tuple(params),
        )


_NODE_COLS = ("bmc_mac", "serial", "host_mac", "order_no", "assigned_order_no",
              "assigned_sales", "assigned_customer", "assigned_population",
              "assigned_serial", "assigned_at")


def read_nodes(bmc_macs=None) -> dict:
    """{bmc_mac: {bmc_mac, serial, host_mac, order_no, assigned_*}} (no vars)."""
    sql = "SELECT " + ", ".join(c + ("::text" if c == "bmc_mac" else "") for c in _NODE_COLS) \
        + " FROM post_node"
    params = ()
    if bmc_macs is not None:
        if not bmc_macs:
            return {}
        sql += " WHERE bmc_mac::text = ANY(%s)"
        params = (list(bmc_macs),)
    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = {}
    for r in rows:
        d = dict(zip(_NODE_COLS, r))
        if d["assigned_at"] is not None and not isinstance(d["assigned_at"], str):
            d["assigned_at"] = d["assigned_at"].isoformat()
        out[d["bmc_mac"]] = d
    return out


def touch_order(conn, order_no, *, sales=None, customer=None) -> None:
    """Open (or reopen) an order in post_order; non-null sales/customer win."""
    if not order_no:
        return
    conn.execute(
        "INSERT INTO post_order (order_no, sales, customer) VALUES (%s, %s, %s) "
        "ON CONFLICT (order_no) DO UPDATE SET finished_at = NULL, "
        "sales = COALESCE(EXCLUDED.sales, post_order.sales), "
        "customer = COALESCE(EXCLUDED.customer, post_order.customer)",
        (order_no, sales, customer))


def open_orders(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT order_no FROM post_order WHERE finished_at IS NULL").fetchall()}


def list_orders(*, include_finished: bool = False) -> list:
    """Registered orders (post_order) plus any order that has finished runs but no
    registry row (e.g. RRORD-100's runs from before migration 042), newest first."""
    sql = ("SELECT order_no, sales, customer, opened_at, finished_at FROM post_order"
           + ("" if include_finished else " WHERE finished_at IS NULL")
           + " UNION ALL SELECT r.order_no, NULL, NULL, min(r.finished_at), NULL FROM post_run r "
           "WHERE r.order_no IS NOT NULL AND r.order_no <> '' "
           "AND NOT EXISTS (SELECT 1 FROM post_order o WHERE o.order_no = r.order_no) "
           "GROUP BY r.order_no")
    sql = "SELECT * FROM (" + sql + ") x ORDER BY opened_at DESC NULLS LAST"
    with get_pool().connection() as conn:
        rows = conn.execute(sql).fetchall()
    iso = lambda v: v.isoformat() if v is not None and not isinstance(v, str) else v
    return [{"order_no": o, "sales": s, "customer": c, "opened_at": iso(a), "finished_at": iso(f)}
            for o, s, c, a, f in rows]


def reassign_entries(conn, bmc_mac, serial, new_order) -> dict:
    """Move this node's entries that sit under an OPEN order to `new_order`
    (one node in at most one open order; spec 2026-11-06). Runs under a
    finished, unregistered or NULL order are never rewritten, and only runs of
    THIS serial move (a moved mezz card's other-blade runs stay). Runs inside
    the caller's transaction."""
    counts = {"node": 0, "runs": 0, "run_rows": 0, "artifacts": 0}
    if not new_order:
        return counts
    opened = list(open_orders(conn))
    mine = {r[0] for r in conn.execute(
        "SELECT run_id FROM post_run WHERE bmc_mac = %s AND serial = %s "
        "UNION SELECT run_id FROM post_artifact WHERE bmc_mac = %s AND serial = %s",
        (bmc_mac, serial, bmc_mac, serial)).fetchall()}
    counts["node"] = conn.execute("UPDATE post_node SET order_no = %s WHERE bmc_mac = %s",
                                  (new_order, bmc_mac)).rowcount
    row = conn.execute("SELECT vars FROM post_node WHERE bmc_mac = %s FOR UPDATE",
                       (bmc_mac,)).fetchone()
    vars_ = (row[0] if row else None) or {}
    changed = False
    for r in [x for x in (vars_.get("runs") or []) if isinstance(x, dict)] + \
            ([vars_["result"]] if isinstance(vars_.get("result"), dict) else []):
        if r.get("order_no") in opened and r.get("run_id") in mine and r["order_no"] != new_order:
            r["order_no"] = new_order
            counts["runs"] += 1
            changed = True
    if changed:
        conn.execute("UPDATE post_node SET vars = %s, generation = generation + 1 WHERE bmc_mac = %s",
                     (json.dumps(vars_), bmc_mac))
    for table, key in (("post_run", "run_rows"), ("post_artifact", "artifacts")):
        counts[key] = conn.execute(
            "UPDATE " + table + " SET order_no = %s WHERE bmc_mac = %s AND serial = %s "
            "AND order_no = ANY(%s) AND order_no <> %s",
            (new_order, bmc_mac, serial, opened, new_order)).rowcount
    return counts


def assign_node(bmc_mac, serial, assignment, settings) -> dict:
    """Write (or clear, assignment=None) a tile's own order and move its
    open-order entries to the resulting effective order, in ONE transaction."""
    from . import assign
    a = assignment or {}
    vals = (a.get("order_no"), a.get("sales"), a.get("customer"), a.get("population"),
            serial if assignment else None)
    with get_pool().connection() as conn, conn.transaction():
        n = conn.execute(
            "UPDATE post_node SET assigned_order_no = %s, assigned_sales = %s, "
            "assigned_customer = %s, assigned_population = %s, assigned_serial = %s, "
            "assigned_at = " + ("NOW()" if assignment else "NULL") + " WHERE bmc_mac = %s",
            vals + (bmc_mac,)).rowcount
        if not n:
            conn.execute(
                "INSERT INTO post_node (bmc_mac, serial, assigned_order_no, assigned_sales, "
                "assigned_customer, assigned_population, assigned_serial, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, " + ("NOW()" if assignment else "NULL") + ")",
                (bmc_mac, serial) + vals)
        node = {"serial": serial, "assigned_order_no": vals[0], "assigned_sales": vals[1],
                "assigned_customer": vals[2], "assigned_population": vals[3],
                "assigned_serial": vals[4]}
        eff = assign.effective(node, settings, serial)
        touch_order(conn, eff["order_no"], sales=a.get("sales"), customer=a.get("customer"))
        moved = reassign_entries(conn, bmc_mac, serial, eff["order_no"])
    return {"order_no": eff["order_no"], "effective": eff, "moved": moved}


def apply_header_order(order_no, followers, *, sales=..., customer=...) -> list:
    """New header order + move every follower's open-order entries, in ONE
    transaction. Followers = assign.header_followers (present, unassigned)."""
    sets, params = ["order_no = %s"], [order_no]
    if sales is not ...:
        sets.append("sales = %s"); params.append(sales)
    if customer is not ...:
        sets.append("customer = %s"); params.append(customer)
    out = []
    with get_pool().connection() as conn, conn.transaction():
        conn.execute("UPDATE post_settings SET " + ", ".join(sets) +
                     ", updated_at = NOW() WHERE id = 1", tuple(params))
        touch_order(conn, order_no, sales=None if sales is ... else sales,
                    customer=None if customer is ... else customer)
        for f in followers:
            c = reassign_entries(conn, f["bmc_mac"], f["serial"], order_no)
            out.append(dict(port=f["port"], serial=f["serial"], **c))
    return out


def touch_header_labels(order_no, *, sales=None, customer=None) -> None:
    """Carry a header sales/customer edit onto the open order's registry row."""
    if not order_no:
        return
    with get_pool().connection() as conn:
        touch_order(conn, order_no, sales=sales, customer=customer)


def runs_under(order_no) -> list:
    """Every finished run under `order_no`: post_run rows (full result) plus compact
    vars.runs entries with no post_run row (serial from their artifacts)."""
    out = []
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT r.bmc_mac, r.serial, r.run_id, r.verdict, "
            "extract(epoch from r.finished_at)::bigint, r.result, n.host_mac "
            "FROM post_run r LEFT JOIN post_node n ON n.bmc_mac = r.bmc_mac "
            "WHERE r.order_no = %s", (order_no,)).fetchall()
        have = set()
        for bmc, sn, rid, verdict, fin, result, host in rows:
            have.add((bmc, rid))
            out.append({"bmc_mac": bmc, "serial": sn, "run_id": rid, "verdict": verdict,
                        "finished_at": fin, "port": (result or {}).get("port"),
                        "result": result, "compact": False, "host_mac": host})
        nodes = conn.execute("SELECT bmc_mac, host_mac, vars->'runs' FROM post_node "
                             "WHERE vars->'runs' IS NOT NULL").fetchall()
        for bmc, host, runs in nodes:
            for r in runs or []:
                if not isinstance(r, dict) or r.get("order_no") != order_no:
                    continue
                if (bmc, r.get("run_id")) in have or not r.get("run_id"):
                    continue
                own = conn.execute("SELECT serial FROM post_artifact WHERE bmc_mac = %s AND "
                                   "run_id = %s AND serial IS NOT NULL LIMIT 1",
                                   (bmc, r["run_id"])).fetchone()
                out.append({"bmc_mac": bmc, "serial": own[0] if own else None,
                            "run_id": r["run_id"], "verdict": r.get("verdict"),
                            "finished_at": r.get("finished_at"), "port": r.get("port"),
                            "result": None, "compact": True, "host_mac": host})
    return out


def artifacts_for(bmc_mac, run_id) -> list:
    with get_pool().connection() as conn:
        rows = conn.execute("SELECT stage, name, kind, content, bytes FROM post_artifact "
                            "WHERE bmc_mac = %s AND run_id = %s ORDER BY stage, name",
                            (bmc_mac, run_id)).fetchall()
    return [{"stage": s, "name": n, "kind": k, "content": c, "bytes": b} for s, n, k, c, b in rows]


def mark_finished(order_no, *, sales, customer, counts) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO post_order (order_no, sales, customer, finished_at, finished_counts) "
            "VALUES (%s, %s, %s, NOW(), %s) ON CONFLICT (order_no) DO UPDATE SET "
            "finished_at = NOW(), finished_counts = EXCLUDED.finished_counts, "
            "sales = COALESCE(EXCLUDED.sales, post_order.sales), "
            "customer = COALESCE(EXCLUDED.customer, post_order.customer)",
            (order_no, sales, customer, json.dumps(counts)))


def order_row(order_no) -> dict:
    with get_pool().connection() as conn:
        r = conn.execute("SELECT sales, customer, finished_at FROM post_order WHERE order_no = %s",
                         (order_no,)).fetchone()
    return {"sales": r[0], "customer": r[1], "finished_at": r[2]} if r else {}


def write_run(bmc_mac, result: dict, *, serial=None, order_no=None) -> None:
    """Upsert the full per-run result into post_run; no run_id -> nothing."""
    run_id = (result or {}).get("run_id")
    if not run_id:
        return
    fin = result.get("finished_at")
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO post_run (bmc_mac, run_id, serial, order_no, verdict, finished_at, result) "
            "VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s) "
            "ON CONFLICT (bmc_mac, run_id) DO UPDATE SET serial = EXCLUDED.serial, "
            "order_no = EXCLUDED.order_no, verdict = EXCLUDED.verdict, "
            "finished_at = EXCLUDED.finished_at, result = EXCLUDED.result",
            (bmc_mac, run_id, serial or result.get("serial"), order_no or result.get("order_no"),
             result.get("verdict"), fin if isinstance(fin, (int, float)) else None,
             json.dumps(result)))


def write_artifact(bmc_mac, run_id, stage, name, kind, content, *,
                   serial=None, order_no=None, nbytes=None) -> None:
    """Upsert one durable evidence artifact, keyed by (bmc_mac, run_id, stage, name).

    NUL bytes are stripped: a SOL capture carries them (serial noise, BREAK)
    and PostgreSQL text rejects them; the raise escaped poll_target after the
    verdict was written (et10b2 2026-09-12)."""
    if isinstance(content, str) and "\x00" in content:
        content = content.replace("\x00", "")
        nbytes = None
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


def get_stage_artifacts(bmc_mac, run_id, stage, names=None) -> dict:
    """{name: {"content", "captured_at"}} for one stage of a node's run, in one
    query; `names` limits it to those artifacts."""
    sql = ("SELECT name, content, captured_at FROM post_artifact "
           "WHERE bmc_mac = %s AND run_id = %s AND stage = %s")
    params = [bmc_mac, run_id, stage]
    if names is not None:
        sql += " AND name = ANY(%s)"; params.append(list(names))
    with get_pool().connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return {n: {"content": c, "captured_at": t} for n, c, t in rows}


def run_owner(run_id) -> "tuple | None":
    """(bmc_mac, serial) of the blade whose artifacts carry `run_id`, or None.
    The slot row's latch names a run but not its blade; the occupant check
    (observe/ipmi.occupant_change) uses this once for rows stamped before
    `occupant` existed."""
    if not run_id:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT bmc_mac, serial FROM post_artifact "
            "WHERE run_id = %s AND serial IS NOT NULL LIMIT 1",
            (run_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def purge_run(bmc_mac, run_id) -> None:
    """Delete all evidence rows for one abandoned qualification run (re-run purge)."""
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM post_artifact WHERE bmc_mac = %s AND run_id = %s",
            (bmc_mac, run_id),
        )


def get_node(bmc_mac) -> dict:
    """The durable post_node row's vars for a blade, or {} if there is no row."""
    with get_pool().connection() as conn:
        rows = conn.execute("SELECT vars FROM post_node WHERE bmc_mac = %s", (bmc_mac,)).fetchall()
    return rows[0][0] if rows and isinstance(rows[0][0], dict) else {}


def update_result(bmc_mac, result: dict) -> None:
    """Refresh vars.result in place for the SAME run (the battery finished
    after a fail verdict, Deploy's tail, a population re-judge): no history entry is
    appended, but that run's history entry takes the result's verdict."""
    runs = [r for r in (get_node(bmc_mac).get("runs") or []) if isinstance(r, dict)]
    rid = (result or {}).get("run_id")
    if rid and any(r.get("run_id") == rid and r.get("verdict") != result.get("verdict") for r in runs):
        runs = [dict(r, verdict=result.get("verdict")) if r.get("run_id") == rid else r for r in runs]
        upsert_node(bmc_mac, result=result, runs=runs)
    else:
        upsert_node(bmc_mac, result=result)
    write_run(bmc_mac, result)


def record_result(bmc_mac, result: dict, *, serial=None, order_no=None, last_port=None) -> None:
    """Write a finished qualification run into the durable tier (spec 2026-09-11 §3):
    vars.result is the latest run, vars.runs the compact history. Read-modify-write
    on runs because the upsert's JSONB merge is shallow."""
    prior = get_node(bmc_mac)
    runs = [r for r in (prior.get("runs") or []) if isinstance(r, dict)]
    runs.append({k: result.get(k) for k in ("run_id", "verdict", "finished_at", "order_no", "port")})
    upsert_node(bmc_mac, serial=serial, order_no=order_no, last_port=last_port,
                result=result, runs=runs)
    write_run(bmc_mac, result, serial=serial, order_no=order_no)


def write_fio_drives(bmc_mac, run_id, rows) -> int:
    """Upsert one run's per-drive fio numbers (flax_post.fio_drives.rows) into
    post_fio_drive, keyed by (bmc_mac, run_id, dev, job, metric). Returns rows written."""
    if not rows:
        return 0
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO post_fio_drive (bmc_mac, run_id, serial, model, size_gb, controller, "
                "dev, job, metric, value) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (bmc_mac, run_id, dev, job, metric) DO UPDATE SET "
                "  serial = EXCLUDED.serial, model = EXCLUDED.model, size_gb = EXCLUDED.size_gb, "
                "  controller = EXCLUDED.controller, value = EXCLUDED.value",
                [(bmc_mac, run_id, r.get("serial"), r.get("model"), r.get("size_gb"),
                  r.get("controller"), r["dev"], r["job"], r["metric"], r["value"]) for r in rows])
    return len(rows)


def fio_peers(keys, exclude_serials) -> dict:
    """Site-wide sibling values for the judge: {(model, size_gb): {(job, metric): [value]}}
    from the latest run of every serial with that key, the given serials excluded (the
    node's own drives, wherever they ran before). Rows without a serial never count."""
    keys = [(m, s) for m, s in dict.fromkeys(keys or []) if m]
    if not keys:
        return {}
    models = [m for m, _ in keys]
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT model, size_gb, job, metric, value FROM ("
            "  SELECT model, size_gb, job, metric, value, serial, captured_at,"
            "         rank() OVER (PARTITION BY serial ORDER BY captured_at DESC, run_id DESC) AS rk"
            "  FROM post_fio_drive"
            "  WHERE serial IS NOT NULL AND model = ANY(%s) AND NOT (serial = ANY(%s))"
            ") x WHERE rk = 1",
            (models, list(exclude_serials or [])),
        ).fetchall()
    want = set(keys)
    out: dict = {}
    for model, size, job, metric, value in rows:
        if (model, size) in want:
            out.setdefault((model, size), {}).setdefault((job, metric), []).append(value)
    return out


def fio_runs() -> list:
    """(bmc_mac, run_id) of every stored run with a fio artifact (the backfill's input)."""
    with get_pool().connection() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT bmc_mac, run_id FROM post_artifact WHERE stage = 'fio' AND name = 'fio' "
            "ORDER BY captured_at").fetchall()]
