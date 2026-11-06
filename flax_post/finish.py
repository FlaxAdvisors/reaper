"""Finish order (spec 2026-11-06 per-node order): archive every node of ONE post
order into flax-control's rackpost.* archive, in rackpost's Close payload shape
(flax_rack/archive.py:payload) with closed_by='post'.

Membership is by COMPLETED RUN: a node belongs to order X once it finished a run
whose order_no == X (state.runs_under), and X archives each node's LATEST run under
X. Inventory comes only from that run's macinv-v, and only when its FRU serial and
BMC MAC match the node (blank beats stale). flax-control stays the single writer.
"""
import base64
import csv
import datetime
import io
import gzip
import hashlib
import json
import os
import re
import urllib.error
import urllib.request

from . import assign, inventory, records

# Same pattern as flax_control.order_archive._PATH_RE (pinned by test; post does
# not import flax_control).
SAFE_PATH = re.compile(r"[A-Za-z0-9._@+=:,-][A-Za-z0-9._@+=:,/-]{0,511}")
MAX_FILE = 5 * 1024 * 1024
MAX_ORDER = 200 * 1024 * 1024
DEFAULT_URL = "http://127.0.0.1:8000"


# rackpost's customer CSVs (flax_rack/report.py), minus `rack` (a post order has none)
DEVICE_COLUMNS = ("pos", "ou", "kind", "part", "serial", "bmc_mac", "host_mac",
                  "fabric_a_mac", "fabric_b_mac", "mgmt_port", "fabric_a_port", "fabric_b_port")
FIRMWARE_COLUMNS = ("pos", "ou", "kind", "serial", "bios_version", "bmc_version",
                    "bmc_rom_version", "nic_firmware", "qualify", "deploy", "ha_mode", "done")
COMPONENT_COLUMNS = ("pos", "kind", "model", "serial", "slot")
CSV_KINDS = ("devices", "firmware", "inventory")


def csv_names(customer, order_no, sales) -> dict:
    """{kind: '<customer>-<order>[-<sales>]-<kind>.csv'}, filename-safe: rackpost's names."""
    tag = f"{customer or 'customer'}-{order_no}" + (f"-{sales}" if sales else "")
    tag = re.sub(r"[^A-Za-z0-9._-]+", "_", tag).strip("_")
    return {k: f"{tag}-{k}.csv" for k in CSV_KINDS}


def _csv_text(columns, rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for row in rows:
        w.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
    return buf.getvalue()


class FinishError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def is_done(run) -> bool:
    """A run that reached Done: a pass verdict (the Done tail identifies + powers off
    only on pass). Failed, unfinished or verdict-less runs are not Done."""
    return (run or {}).get("verdict") == "pass"


def members(runs):
    """(members, left_out, no_serial) for one order (operator 2026-11-06).

    Per node (serial), its LATEST run under the order decides: Done (pass) ->
    member, archived with that run; anything else (failed, unfinished) ->
    left_out, never archived, even if an earlier run under the order passed --
    the archive must be the node's current state. Nodes Done long ago (and
    gone from the rack) are members. no_serial: unusable serials, never archived."""
    latest, no_serial = {}, []
    for r in runs:
        sn = r.get("serial")
        if records.serial_fault(sn):
            no_serial.append(r)
            continue
        cur = latest.get(sn)
        if cur is None or (r.get("finished_at") or 0) > (cur.get("finished_at") or 0):
            latest[sn] = r
    mems = [r for r in latest.values() if is_done(r)]
    left = [dict(r, why=(r.get("verdict") or "not done")) for r in latest.values() if not is_done(r)]
    return (sorted(mems, key=lambda r: r["serial"]),
            sorted(left, key=lambda r: r["serial"]), no_serial)


def running(order_no, slots, member_serials):
    """Nodes in the rack under this order that are not Done yet (not archived)."""
    return [{"port": s.get("port"), "serial": s.get("serial"), "why": "running"}
            for s in slots or []
            if s.get("order_no") == order_no and s.get("serial")
            and s.get("phase") != "Done" and s.get("serial") not in member_serials]


def _iso(epoch):
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).isoformat()


def _norm(mac):
    return (mac or "").replace(":", "").replace("-", "").replace(".", "").lower()


def _blob(content):
    raw = content.encode("utf-8")
    return raw, {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                 "content_type": "text/plain",
                 "gzip_b64": base64.b64encode(gzip.compress(raw)).decode()}


def build_payload(order_no, mems, *, customer, sales, site, get_artifacts, closed_at=None) -> dict:
    devices, macs, comps, rungs, arts = [], [], [], [], []
    dev_rows, fw_rows, inv_rows = [], [], []
    total = 0
    for m in mems:
        sn, run_id, port = m["serial"], m["run_id"], m.get("port")
        result = m.get("result") or {}
        files = get_artifacts(m["bmc_mac"], run_id) or []
        by_name = {f["name"]: f for f in files}
        parsed = inventory.parse((by_name.get("macinv-v") or {}).get("content") or "")
        fin = _iso(m.get("finished_at"))
        verdict = m.get("verdict")
        if verdict and result.get("clean") is False:
            verdict += " (not clean)"
        raw = {}
        stage_paths = {}
        for f in files:
            path = f"{sn}/{run_id}/{f['stage']}/{f['name']}"
            stage_paths.setdefault(f["stage"], []).append(path)
            if f["name"] == "ipmitool-fru":
                raw["ipmitool-fru"] = path
            if not SAFE_PATH.fullmatch(path) or ".." in path.split("/") or "//" in path:
                continue                     # stays in its rung; viewer shows "not archived"
            content = f.get("content")
            if content is None:
                arts.append({"path": path, "status": "missing", "size": 0})
                continue
            data, meta = _blob(content)
            if len(data) > MAX_FILE or total + len(data) > MAX_ORDER:
                arts.append({"path": path, "status": "too_large", "size": len(data)})
                continue
            total += len(data)
            arts.append(dict(path=path, status="stored", **meta))
        if raw.get("ipmitool-fru") and not SAFE_PATH.fullmatch(raw["ipmitool-fru"]):
            raw = {}
        devices.append({
            "pos": sn, "ou": None, "kind": "server",
            "part": (parsed.get("fru") or {}).get("board_product"),
            "hostname": assign.hostname(order_no, port) if port else None,
            "chassis_serial": sn, "final_verdict": verdict, "ha_mode": None,
            "identity": {"title": "FRU 0", "serial": sn,
                         "rows": [["BMC MAC", m["bmc_mac"]], ["run", run_id],
                                  ["port", port or ""], ["finished", fin or ""]],
                         "raw": raw}})
        for role, mac in (("bmc", m.get("bmc_mac")), ("host", m.get("host_mac"))):
            if mac:
                macs.append({"pos": sn, "role": role, "mac": mac})
        fru = parsed.get("fru") or {}
        if fru.get("serial") == sn and _norm((parsed.get("bmc") or {}).get("mac")) == _norm(m["bmc_mac"]):
            for kind in ("cpu", "memory", "nic", "storage", "gpu"):
                for c in parsed.get(kind) or []:
                    comp = {"kind": kind, "model": c.get("model") or c.get("part") or c.get("size"),
                            "serial": c.get("serial"), "slot": c.get("slot")}
                    comps.append(dict(comp, pos=sn))
                    inv_rows.append(dict(comp, pos=port or sn))
        fw = result.get("fw") or {}
        nic_fw = "; ".join(sorted({d.get("current") for d in (fw.get("nic") or {}).get("devices") or []
                                   if d.get("current")}))
        bmc_ver = (fw.get("bmc") or {}).get("current")
        if not bmc_ver or bmc_ver == "report-only":
            bmc_ver = ((parsed.get("bmc") or {}).get("version") or "").split()[:1] or [bmc_ver]
            bmc_ver = bmc_ver[0]
        dev_rows.append({"pos": port or sn, "kind": "server",
                         "part": (parsed.get("fru") or {}).get("board_product"), "serial": sn,
                         "bmc_mac": m.get("bmc_mac"), "host_mac": m.get("host_mac")})
        fw_rows.append({"pos": port or sn, "kind": "server", "serial": sn,
                        "bios_version": (fw.get("bios") or {}).get("current")
                        or (parsed.get("bios") or {}).get("version"),
                        "bmc_version": bmc_ver, "nic_firmware": nic_fw or None,
                        "qualify": verdict, "done": fin})
        steps = ((result.get("qual") or {}).get("steps") or {})
        for name in sorted(set(steps) | set(stage_paths)):
            rungs.append({"pos": sn, "stage": "Qualify", "rung": name,
                          "verdict": (steps.get(name) or {}).get("status"),
                          "finished_at": fin, "artifacts": sorted(stage_paths.get(name, []))})
        rungs.append({"pos": sn, "stage": "Done", "rung": "done",
                      "verdict": (result.get("done") or {}).get("verdict") or m.get("verdict"),
                      "finished_at": fin, "artifacts": []})
    names = csv_names(customer, order_no, sales)
    for kind, cols, rows_ in (("devices", DEVICE_COLUMNS, dev_rows),
                              ("firmware", FIRMWARE_COLUMNS, fw_rows),
                              ("inventory", COMPONENT_COLUMNS, inv_rows)):
        _data, meta = _blob(_csv_text(cols, rows_))
        meta["content_type"] = "text/csv"
        arts.append(dict(path=names[kind], status="stored", **meta))
    return {"order_id": order_no, "rack": "", "customer": customer, "site": site,
            "sales": sales or None,
            "closed_at": closed_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "order_dir": None, "closed_by": "post",
            "devices": devices, "macs": macs, "components": comps, "rungs": rungs,
            "artifacts": arts}


def send(payload, *, base_url=None, opener=urllib.request.urlopen) -> dict:
    url = (base_url or os.environ.get("FLAX_CONTROL_URL", DEFAULT_URL)).rstrip("/") \
        + "/api/v1/order-archive"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error") or str(e)
        except Exception:
            msg = str(e)
        raise FinishError(e.code if e.code in (400, 409) else 502, msg) from None
    except (urllib.error.URLError, OSError) as e:
        raise FinishError(502, "flax-control unreachable: %s" % e) from None
