"""The archive payload Close sends to flax-control (POST /api/v1/order-archive).

flax-control replaces this (order_id, rack)'s rows in the rackpost.* tables
(migration 037) in one transaction; rackpost never writes Postgres itself.

  devices     report.device_rows' rows (geometry devices, JBOD IOMs, switches) with
              final_verdict: "deployed" when the device's Deploy passed, else
              "not deployed: <deploy_why>"; "n/a" for a device without Deploy rungs
              (JBOD, IOM, switch). identity: the drawer's identity block as rendered
              rows (identity_block: the latest `serial` read of that pos -- FRU 0,
              fruid-util, weutil, syseeprom), None before Discover read one. An IOM's
              or switch's chassis_serial is that read's serial (they have no Qualify
              inventory); a host's stays the inventory's (never backfilled)
  macs        {pos, role, mac}: host / bmc of a device, iom<n> of an IOM row, switch of
              a switch row, fabric_a / fabric_b (report.device_rows: a passed ha-config,
              else Qualify's ha-path ports). Blanks skipped.
  components  report.latest_components: each device's LATEST Qualify inventory's parts
              (Done's rows only when they came from that run; never with an older run's
              parts beside the latest run's chassis serial)
  rungs       the latest run per (pos, stage, rung): verdict, finished_at and the
              artifact PATHS (relative to order_dir). Never the artifact text: logs stay
              in the order dir (spec §8.2).
"""
from datetime import datetime, timezone

from . import devview, report
from .deploy import deploy_rungs, deploy_why

# the drawer's identity facts beside the read's own rows (rackpost.js identRows)
ID_LABELS = (("product_name", "product"), ("board_product", "board product"),
             ("board_serial", "board serial"), ("part_number", "part number"),
             ("service_tag", "service tag"), ("asset_tag", "asset tag"),
             ("local_mac", "local MAC (BMC)"), ("ext_mac_base", "ext MAC base"),
             ("base_mac", "base MAC"), ("syseeprom_serial", "SONiC syseeprom serial"),
             ("platform", "platform"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _final(d, why) -> str:
    if d is None or not deploy_rungs(d):
        return "n/a"
    return f"not deployed: {why[d.pos]}" if d.pos in why else "deployed"


def identity_block(ident: dict | None) -> dict | None:
    """devview.identity -> {title, serial, rows: [[label, value]], raw: {name: path}}:
    the facts the read's own rows do not already show, then those rows (the drawer's
    order). raw = the read's artifact PATHS (relative to order_dir), never their text."""
    if not ident:
        return None
    rows = [[str(r[0]), str(r[1])] for r in ident.get("rows") or []
            if isinstance(r, (list, tuple)) and len(r) == 2]
    shown = {v.split(" (")[0].lower() for _, v in rows}
    extra = [[label, str(ident[k])] for k, label in ID_LABELS
             if ident.get(k) and str(ident[k]).lower() not in shown]
    raw = {str(k): str(v) for k, v in (ident.get("artifacts") or {}).items()}
    if not (rows or extra or raw or ident.get("serial")):
        return None                                 # a read that carried nothing
    return {"title": ident.get("serial_field") or "serial",
            "serial": ident.get("serial") or None, "rows": extra + rows, "raw": raw}


def payload(g, db, order_dir, *, closed_at: str | None = None) -> dict:
    lr = db.latest_runs()
    by_pos: dict[str, dict] = {}
    for (pos, rung), run in lr.items():
        by_pos.setdefault(pos, {})[rung] = run
    why = deploy_why(lr, g)
    geo = {d.pos: d for d in g.devices}
    devices, macs = [], []
    for row in report.device_rows(order_dir, g, db):
        pos = row["pos"]
        ident = identity_block(devview.identity(by_pos.get(pos, {})))
        serial = row["chassis_serial"] or None
        if serial is None and pos not in geo:       # an IOM / switch: its own read
            serial = (ident or {}).get("serial")
        devices.append({
            "pos": pos, "ou": row["ou"] if isinstance(row["ou"], int) else None,
            "kind": row["kind"], "part": row["part"], "hostname": row["hostname"],
            "chassis_serial": serial,
            "final_verdict": _final(geo.get(pos), why),
            "ha_mode": row["ha_mode"] or None, "identity": ident})
        if row["kind"] == "iom":
            roles = (("iom" + pos.rsplit(".iom", 1)[1], "bmc_mac"),)
        elif pos.startswith("_sw-"):
            roles = (("switch", "bmc_mac"),)
        else:
            roles = (("host", "host_mac"), ("bmc", "bmc_mac"))
        roles += (("fabric_a", "fabric_a_mac"), ("fabric_b", "fabric_b_mac"))
        macs += [{"pos": pos, "role": role, "mac": row[col]} for role, col in roles if row[col]]
    comps = report.latest_components(order_dir, db, lr)
    rungs = [{"pos": r["pos"], "stage": r["stage"], "rung": r["rung"],
              "verdict": r["verdict"], "finished_at": r["finished_at"],
              "artifacts": sorted(r["artifacts"].values())}
             for r in db.latest_stage_runs()]
    return {"order_id": g.order, "rack": g.rack, "customer": g.customer, "site": g.site,
            "sales": g.sales or None,
            "closed_at": closed_at or now(), "order_dir": str(order_dir),
            "devices": devices, "macs": macs, "components": comps, "rungs": rungs}
