"""flax-post — Eindhoven Post Servers viewer (read-only, source='post' devices)."""
import time
import logging
import re
import os
from datetime import timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import actions, assign, blades, finish, blocklist, consume, db, geometry, inventory, population, queries, records, state, stepinfo
from .observe import host_qual
from .qualclient import QualClient, QualUnreachable
from .version import __version__

log = logging.getLogger("flax-post.app")

BASE_DIR = Path(__file__).parent

app = FastAPI(title="flax-post", version=__version__)
WEB_DIR = BASE_DIR / "web"
app.mount("/web-static", StaticFiles(directory=str(WEB_DIR / "static")), name="web-static")
rack_templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@app.middleware("http")
async def _static_no_cache(request: Request, call_next):
    """Our own static assets revalidate on every load (ETag/304 is cheap): a
    browser holding a cached app.js across a release rendered the OLD phase
    bar for hours (2026-09-12). Vendor files are versioned by path."""
    resp = await call_next(request)
    if request.url.path.startswith("/web-static/") and "/vendor/" not in request.url.path:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _rackname() -> str:
    """Bang rack name for the page title; /etc/rackname is /etc/hostname mounted."""
    try:
        with open(os.environ.get("FLAX_RACKNAME_PATH", "/etc/rackname")) as f:
            return f.read().strip() or "post"
    except OSError:
        return "post"


def _site() -> str:
    """Operator-facing site name for the rack title (e.g. 'Eindhoven').

    Reads SITE_NAME from /etc/flax/site.env (mounted into the container); falls
    back to the rackname. Capitalized for display — the title is '<Site> Post
    Servers', not '<hostname> Post Servers'.
    """
    try:
        with open(os.environ.get("FLAX_SITE_ENV", "/etc/flax/site.env")) as f:
            for line in f:
                if line.startswith("SITE_NAME="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v.capitalize()
    except OSError:
        pass
    return _rackname().capitalize()


def _ctx(**extra) -> dict:
    return {"version": __version__, "rackname": _rackname(), "site": _site(), **extra}


def _blade_slots() -> list[dict]:
    geo = geometry.load_geometry()
    switch = blades.post_switch(geo)
    devices = queries.post_devices()
    facts = consume.switch_ports(switch)
    consumed = consume.consumed_by_port(devices, facts, switch)
    try:
        orders = state.order_labels()
    except Exception:          # e.g. before migration 044: blank labels, never a dead view
        log.exception("post_order labels unreadable; labels blank this pass")
        orders = {}
    return blades.build_slots(geo["slots"], consumed, state.read_state(),
                              state.read_settings(), facts, nodes=state.read_nodes(),
                              orders=orders)


@app.get("/")
def index(request: Request):
    settings = state.read_settings()
    switch = blades.post_switch(geometry.load_geometry())
    return rack_templates.TemplateResponse(
        request, "rack.html",
        _ctx(switch=switch, phases=list(blades.PHASES), settings=settings))


@app.get("/api/v1/orders")
def api_orders(include_finished: int = 0) -> JSONResponse:
    """Open post orders (post_order registry, newest first) + the header order:
    the Order field's datalist and the Finish dialog's choices. With
    include_finished=1, finished orders too (the UI's label refill index)."""
    return JSONResponse({"orders": state.list_orders(include_finished=bool(include_finished)),
                         "header": state.read_settings().get("order_no")})


_SALES_RE = re.compile(r"^[A-Za-z0-9._-]{0,64}$")
_LABEL_KEYS = ("sales", "customer", "population")


def _labels_in(body) -> "tuple[dict, str | None]":
    """The label fields present in `body`, stripped ('' -> None), and an error
    string when one is invalid (sales: the archive's rule; any field <= 64)."""
    out = {}
    for k in _LABEL_KEYS:
        if k in body:
            v = (str(body[k]) if body[k] is not None else "").strip()
            if len(v) > 64:
                return {}, f"{k} longer than 64"
            if k == "sales" and not _SALES_RE.match(v):
                return {}, "sales: letters, digits, . _ - only"
            out[k] = v or None
    return out, None


@app.post("/api/v1/orders/labels")
async def api_order_labels(request: Request) -> JSONResponse:
    """Edit one ORDER's labels (every node on it): overwrite, blank clears.
    A finished order is allowed; the reply says so (re-Finish to re-archive)."""
    body = await request.json()
    order_no = (body.get("order_no") or "").strip()
    if not order_no:
        return JSONResponse({"ok": False, "reason": "no order"}, status_code=400)
    labels, err = _labels_in(body)
    if err:
        return JSONResponse({"ok": False, "reason": err}, status_code=400)
    # Labels never create an order: only Assign or Set opens one.
    if not await run_in_threadpool(state.order_exists, order_no):
        return JSONResponse({"ok": False, "reason": f"no order {order_no}: Assign or Set first"},
                            status_code=404)
    await run_in_threadpool(lambda: state.set_order_labels(order_no, **labels))
    row = await run_in_threadpool(state.order_row, order_no)
    return JSONResponse({"ok": True, "finished": bool(row.get("finished_at"))})


def _finish_context(order_no):
    """Members = every node Done under the order, ever (operator 2026-11-06);
    not-Done nodes are listed but never archived."""
    runs = state.runs_under(order_no)
    mems, left, no_serial = finish.members(runs)
    slots = [s for s in _blade_slots() if not s.get("empty")]
    left = left + finish.running(order_no, slots, {m["serial"] for m in mems} | {l["serial"] for l in left})
    # The order's own record only: run snapshots and the header are history,
    # not labels (11-06..09 runs carry a wrong sales on RRORD-100).
    row = state.order_row(order_no)
    return mems, left, no_serial, row.get("customer"), row.get("sales")


def _brief(m):
    return {"serial": m.get("serial"), "port": m.get("port"), "bmc_mac": m.get("bmc_mac"),
            "run_id": m.get("run_id"), "verdict": m.get("verdict"),
            "finished_at": m.get("finished_at"), "why": m.get("why")}


@app.get("/api/v1/orders/finish-preview")
def api_finish_preview(order: str) -> JSONResponse:
    mems, left, no_serial, customer, sales = _finish_context(order)
    return JSONResponse({"order_no": order, "customer": customer, "sales": sales,
                         "members": [_brief(m) for m in mems],
                         "left_out": [_brief(m) for m in left],
                         "no_serial": [_brief(m) for m in no_serial]})


@app.post("/api/v1/orders/finish")
async def api_finish(request: Request) -> JSONResponse:
    """Archive one post order: every node Done under it (its most recent Done run),
    through flax-control. Not-Done nodes are never archived. The order is marked
    finished only after flax-control accepted the archive."""
    body = await request.json()
    order_no = (body.get("order_no") or "").strip()
    customer = (body.get("customer") or "").strip()
    sales = (body.get("sales") or "").strip() or None
    if not order_no:
        return JSONResponse({"ok": False, "reason": "no order"}, status_code=400)
    if not customer:
        return JSONResponse({"ok": False, "reason": "customer required"}, status_code=400)
    mems, left, _ns, _c, _s = await run_in_threadpool(_finish_context, order_no)
    if not mems:
        return JSONResponse({"ok": False, "reason": "no Done nodes under this order"},
                            status_code=400)
    payload = await run_in_threadpool(finish.build_payload, order_no, mems, customer=customer,
                                      sales=sales, site=_site(), get_artifacts=state.artifacts_for)
    try:
        counts = await run_in_threadpool(finish.send, payload)
    except finish.FinishError as e:
        return JSONResponse({"ok": False, "reason": e.message}, status_code=e.status)
    state.mark_finished(order_no, sales=sales, customer=customer, counts=counts)
    return JSONResponse({"ok": True, "archived": counts, "left_out": len(left)})


@app.get("/api/v1/blades")
def api_blades() -> JSONResponse:
    geo = geometry.load_geometry()
    # The operator context rides on every poll: order, population and customer
    # are set in ONE browser and must show in EVERY open browser within a poll
    # (ruling 2026-09-12), not only on the next page load. So does the build:
    # a page rendered by an older one reloads itself (app.js checkVersion).
    return JSONResponse({"switch": blades.post_switch(geo), "racks": geo["racks"],
                         "slots": _blade_slots(), "settings": state.read_settings(),
                         "version": __version__})


@app.get("/api/v1/profiles")
def api_profiles() -> JSONResponse:
    return JSONResponse({"profiles": population.list_profiles()})


# The run's inventory artifacts the modal reads: macinv's detail form (INV
# tables), its count form (POP verdict), and dimmsum (blocked DIMMs).
_INV_ARTIFACTS = ("macinv-v", "macinv", "dimmsum")
_NO_FULL_INVENTORY = "no full inventory in this run (re-run the blade)"


@app.get("/api/v1/inventory/{port}")
def api_inventory(port: str, profile: "str | None" = None) -> JSONResponse:
    """INV tables and the POP verdict from the blade's CURRENT RUN only: the
    post agent runs ghost `macinv -p` on the node and uploads the detail form
    (`macinv-v` -> parse()) and the count form (`macinv` -> verdict(), the
    same text the pipeline's population-check judged). Never triage's
    /export recon tree: it is keyed by the mezz NIC MAC, which moves between
    blades (et28b4 2026-09-25 showed another blade's 09-17 dump). A run from
    before the agent uploaded `macinv-v` gets empty tables and a reason."""
    record = next((s for s in _blade_slots() if s.get("port") == port), None)
    if record is None:
        return JSONResponse({"ok": False, "reason": "unknown port"}, status_code=404)
    run_id, bmc_mac = record.get("run_id"), record.get("bmc_mac")
    if not run_id or not bmc_mac:
        return JSONResponse({"present": False, "port": port, "reason": "no run"})
    try:
        rows = state.get_stage_artifacts(bmc_mac, run_id, "inventory", names=_INV_ARTIFACTS) or {}
    except Exception:
        log.exception("inventory artifact read failed for %s", port)
        return JSONResponse({"present": False, "port": port, "reason": "artifact read failed",
                             "run_id": run_id})

    def text(name):
        return (rows.get(name) or {}).get("content") or None

    detail = text("macinv-v")
    sections = inventory.parse(detail or "")
    times = [r["captured_at"] for r in rows.values() if r.get("captured_at")]
    out = {"present": True, "port": port, "run_id": run_id,
           "captured_at": _utc_stamp(max(times)) if times else None, "sections": sections}
    if not detail:
        out["reason"] = _NO_FULL_INVENTORY
    # Blocked DIMMs (blocklist): judged on this run's dimmsum; both memory
    # tables paint the matching rows red and the INV button goes red.
    dimms = text("dimmsum")
    out["blocked"] = blocklist.check_dimmsum(dimms) if dimms else blocklist.check(sections.get("memory") or [])
    # The profile the RUN judged this node with (pop_override -> the node's
    # effective population -> header); ?profile= is the operator's what-if.
    from .observe.host_qual import _read_profile_for
    prof = profile or _read_profile_for(record, state)
    count = text("macinv")
    if count:
        out["pop"] = dict(inventory.verdict(count, prof), source="run", run_id=run_id)
    else:
        out["pop"] = {"state": "grey", "profile": prof, "results": [], "missing": [],
                      "source": "run", "run_id": run_id, "reason": "no population digest in this run"}
    return JSONResponse(out)


def _utc_stamp(t) -> str:
    """'YYYY-MM-DD HH:MM:SS UTC' for a timestamptz."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@app.get("/api/v1/step")
def api_step(port: str, phase: str, step: str) -> JSONResponse:
    """The step modal's evidence block (stepinfo.detail) for one blade step:
    status, headline, label/value rows, notes. Computed on click, not per poll."""
    record = next((s for s in _blade_slots() if s.get("port") == port), None)
    if record is None:
        return JSONResponse({"ok": False, "reason": "unknown port"}, status_code=404)
    row = state.read_state().get(port) or {}
    run_id = (row.get("qual") or {}).get("run_id") or record.get("run_id")

    def get_artifact(stage, name):
        if not (record.get("bmc_mac") and run_id):
            return None
        return state.get_artifact(record["bmc_mac"], run_id, stage, name)
    return JSONResponse(stepinfo.detail(record, row, phase, step, get_artifact=get_artifact))


@app.post("/api/v1/settings")
async def api_settings(request: Request) -> JSONResponse:
    """The header's one Set: {order_no?, sales?, customer?, population?}. The
    labels present are written onto the TARGET order's record (the body's
    order, else the current header order) in the same transaction as the
    header change (state.set_header). An ORDER change moves every present,
    unassigned node's open-order entries with it, so it is confirmed first:
    409 lists the movers and the four values to set until confirm: true."""
    body = await request.json()
    status, payload = await run_in_threadpool(_apply_settings, body)
    return JSONResponse(payload, status_code=status)


def _apply_settings(body) -> "tuple[int, dict]":
    """api_settings' work, off the event loop (polls keep answering). The write
    is one transaction in state.set_header, which refuses if the header order
    moved since `current` was read (the followers were computed against it)."""
    labels, err = _labels_in(body)
    if err:
        return 400, {"ok": False, "reason": err}
    current = state.read_settings()
    cur_order = current.get("order_no") or None
    target = ((body.get("order_no") or "").strip() or None) if "order_no" in body else cur_order
    followers = []
    if target != cur_order:
        followers = assign.header_followers(state.read_state(), state.read_nodes(), current)
        if followers and body.get("confirm") is not True:
            rec = (state.order_row(target) if target else {}) or {}
            return 409, {"ok": False, "confirm": [
                {"port": f["port"], "serial": f["serial"],
                 "from": f.get("order_no") or cur_order, "to": target}
                for f in followers],
                "set": dict({k: rec.get(k) for k in _LABEL_KEYS}, order_no=target, **labels)}
    try:
        moved = state.set_header(target, followers, expect_order=cur_order, **labels)
    except state.HeaderMoved:
        return 409, {"ok": False, "reason": "header changed elsewhere; reload"}
    return 200, {"ok": True, "moved": moved, "settings": state.read_settings()}


@app.post("/api/v1/assign")
async def api_assign(request: Request) -> JSONResponse:
    """A tile's own order + population ({port, order_no, population?}) or
    {port, clear: true} to follow the header again. Sales/customer in the body
    (an older open tab) are ignored: they belong to the order (/orders/labels).
    Moves the node's open-order entries to its new effective order in the same
    transaction."""
    body = await request.json()
    port = body.get("port")
    record = next((s for s in _blade_slots() if s.get("port") == port), None)
    if record is None:
        return JSONResponse({"ok": False, "reason": "unknown port"}, status_code=404)
    serial, bmc = record.get("serial"), record.get("bmc_mac")
    if records.serial_fault(serial) or not bmc:
        return JSONResponse({"ok": False, "reason": "no serial"}, status_code=400)
    if body.get("clear") is True:
        assignment = None
    else:
        order_no = (body.get("order_no") or "").strip()
        if not order_no:
            return JSONResponse({"ok": False, "reason": "no order"}, status_code=400)
        assignment = {"order_no": order_no,
                      "population": (body.get("population") or "").strip() or None}
    out = state.assign_node(bmc, serial, assignment, state.read_settings(),
                            orders=state.order_labels())
    return JSONResponse({"ok": True, "effective": out["effective"], "moved": out["moved"]})


@app.post("/api/v1/power")
async def api_power(request: Request) -> JSONResponse:
    body = await request.json()
    port, action = body.get("port"), body.get("action")
    record = next((s for s in _blade_slots() if s.get("port") == port), None)
    if record is None:
        return JSONResponse({"ok": False, "reason": "unknown port"}, status_code=400)
    bmc_ip = record.get("bmc_ip")
    if not bmc_ip:
        return JSONResponse({"ok": False, "reason": "no bmc_ip for port"}, status_code=400)
    blocked = action == "off" and actions.flash_active(record)
    try:
        result = await run_in_threadpool(actions.run_power, bmc_ip, action, blocked=blocked)
    except ValueError as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=400)
    if result.get("blocked"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result)


@app.post("/api/v1/identify")
async def api_identify(request: Request) -> JSONResponse:
    body = await request.json()
    port, mode = body.get("port"), body.get("mode")
    bmc_ip = actions.bmc_ip_for_port(port, _blade_slots())
    if not bmc_ip:
        return JSONResponse({"ok": False, "reason": "unknown port or no bmc_ip"}, status_code=400)
    try:
        result = await run_in_threadpool(actions.run_identify, bmc_ip, mode)
    except ValueError as e:
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=400)
    return JSONResponse(result)


def _target_for(port: str) -> "dict | None":
    """Blade record for a port with the fields host_qual/QualClient need, or None."""
    rec = next((s for s in _blade_slots() if s.get("port") == port), None)
    if rec is None:
        return None
    return {"port": port, "host_ip": rec.get("host_ip"), "bmc_ip": rec.get("bmc_ip"),
            "bmc_mac": rec.get("bmc_mac"), "serial": rec.get("serial"),
            "order_no": rec.get("order_no"), "sales": rec.get("sales"),
            "customer": rec.get("customer"), "population": rec.get("population"),
            "run_id": rec.get("run_id")}


@app.get("/api/v1/artifact")
def api_artifact(port: str, stage: "str | None" = None, name: "str | None" = None) -> JSONResponse:
    t = _target_for(port)
    if t is None:
        return JSONResponse({"ok": False, "reason": "unknown port"}, status_code=404)
    if name is None:
        arts = state.list_artifacts(t["bmc_mac"], t["run_id"], stage)
        return JSONResponse({"port": port, "run_id": t["run_id"], "artifacts": arts})
    content = state.get_artifact(t["bmc_mac"], t["run_id"], stage, name)
    return JSONResponse({"port": port, "stage": stage, "name": name, "content": content})


@app.get("/api/v1/qual/live")
async def api_qual_live(port: str, stage: str) -> JSONResponse:
    t = _target_for(port)
    if t is None or not t.get("host_ip"):
        return JSONResponse({"ok": False, "reason": "unknown port or no host_ip"}, status_code=404)
    client = QualClient(f"http://{t['host_ip']}:8087")
    try:
        detail = await run_in_threadpool(client.stage, stage)
    except QualUnreachable:
        return JSONResponse({"unreachable": True, "port": port, "stage": stage})
    return JSONResponse(detail)


@app.post("/api/v1/qual/restart")
async def api_qual_restart(request: Request) -> JSONResponse:
    body = await request.json()
    t = _target_for(body.get("port"))
    if t is None or not t.get("host_ip"):
        return JSONResponse({"ok": False, "reason": "unknown port or no host_ip"}, status_code=400)
    result = await run_in_threadpool(host_qual.restart_target, t)
    return JSONResponse(result)


@app.post("/api/v1/deploy/retry")
async def api_deploy_retry(request: Request) -> JSONResponse:
    """RETRY a failed Deploy (spec 2026-11-06 loose-post-deploy-stage): mode `rerun`
    (keep the disk: first boot, then the phases that did not pass) or `reinstall`
    (wipe + install, then the rest). Writes vars.deploy_request only;
    flax-post-deployd owns vars.deploy and picks the request up."""
    body = await request.json()
    port, mode = body.get("port"), body.get("mode")
    if mode not in ("rerun", "reinstall"):
        return JSONResponse({"ok": False, "reason": "mode must be rerun or reinstall"}, status_code=400)
    row = state.read_state().get(port)
    if row is None:
        return JSONResponse({"ok": False, "reason": "unknown port"}, status_code=404)
    dep = row.get("deploy") or {}
    if dep.get("state") not in ("fail", "interrupted") or \
            dep.get("run_id") != (row.get("qual") or {}).get("run_id"):
        return JSONResponse({"ok": False, "reason": "no failed Deploy on this run"}, status_code=409)
    state.set_state(port, deploy_request={"mode": mode, "at": time.time()})
    return JSONResponse({"ok": True, "mode": mode})


@app.get("/api/v1/healthz")
def healthz() -> JSONResponse:
    try:
        with db.get_pool().connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok", "db": "ok"})
    except Exception:
        return JSONResponse({"status": "fail", "db": "fail"}, status_code=503)
