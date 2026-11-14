"""The one effective-order rule for a post node (spec 2026-11-06 per-node order).

A tile may carry its own assignment (order + population) on post_node.assigned_*;
the header (post_settings) is the default. Sales/customer are the order's own
(post_order), never the node's. The assignment
applies only while its assigned_serial equals the node's (vouched) serial: a moved
mezz card carries the BMC MAC to another blade, which must follow the header and
show the assignment as stale. Pure: no DB, no imports of other lanes.
"""

_FIELDS = ("order_no", "sales", "customer", "population")


def effective(node, settings, serial=None, orders=None) -> dict:
    """{order_no, sales, customer, population, source, stale, assigned_at}.

    order_no: the node's own assignment while it applies, else the header's.
    sales/customer: that ORDER's record (`orders`, state.order_labels()) only;
    None when it has no record or `orders` is None. population: the node's own
    override -> the order's record -> the header's (spec 2026-11-09)."""
    node = node or {}
    settings = settings or {}
    sn = serial if serial is not None else node.get("serial")
    applies = bool(node.get("assigned_order_no")) and node.get("assigned_serial") is not None \
        and node.get("assigned_serial") == sn
    order_no = node["assigned_order_no"] if applies else settings.get("order_no")
    rec = (orders or {}).get(order_no) or {}
    population = (node.get("assigned_population") if applies else None) \
        or rec.get("population") or settings.get("population")
    return {"order_no": order_no, "sales": rec.get("sales"), "customer": rec.get("customer"),
            "population": population, "source": "node" if applies else "header",
            "stale": bool(node.get("assigned_order_no")) and not applies,
            "assigned_at": node.get("assigned_at")}


def order_prefix(order_no) -> str:
    """lower(first-two-alpha + last-three-digit): 'RRORD-1033' -> 'rr033'; falsy -> ''.
    Same rule as flax_classify.post_reserve.order_prefix (parity pinned by test)."""
    if not order_no:
        return ""
    letters = "".join(c for c in order_no if c.isalpha())[:2]
    digits = "".join(c for c in order_no if c.isdigit())[-3:]
    return (letters + digits).lower()


def hostname(order_no, port):
    """'<prefix>-<port>' or None when the order gives no prefix."""
    p = order_prefix(order_no)
    return f"{p}-{port}" if p else None


def header_followers(state_rows, nodes, settings) -> list:
    """Current slot occupants whose effective order is the header's: the nodes a
    header order change moves. A departed node (no post_state row) is not one,
    so its runs stay in their order until that order is Finished."""
    out = []
    for port, row in sorted((state_rows or {}).items()):
        bmc, sn = row.get("bmc_mac"), row.get("serial")
        if not bmc or not sn:
            continue
        if effective((nodes or {}).get(bmc), settings, sn)["source"] == "header":
            out.append({"port": port, "bmc_mac": bmc, "serial": sn,
                        "order_no": row.get("order_no")})
    return out
