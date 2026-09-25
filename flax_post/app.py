"""flax-post — Eindhoven Post Servers viewer (read-only, source='post' devices)."""
import logging
import os
from datetime import timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import actions, blades, blocklist, consume, db, geometry, inventory, population, queries, state, stepinfo
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
    return blades.build_slots(geo["slots"], consumed, state.read_state(),
                              state.read_settings(), facts)


@app.get("/")
def index(request: Request):
    settings = state.read_settings()
    switch = blades.post_switch(geometry.load_geometry())
    return rack_templates.TemplateResponse(
        request, "rack.html",
        _ctx(switch=switch, phases=list(blades.PHASES), settings=settings))


@app.get("/api/v1/blades")
def api_blades() -> JSONResponse:
    geo = geometry.load_geometry()
    # The operator context rides on every poll: order, population and customer
    # are set in ONE browser and must show in EVERY open browser within a poll
    # (ruling 2026-09-12), not only on the next page load.
    return JSONResponse({"switch": blades.post_switch(geo), "racks": geo["racks"],
                         "slots": _blade_slots(), "settings": state.read_settings()})


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
    prof = profile or state.read_settings().get("population")
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
    return JSONResponse(stepinfo.detail(record, row, phase, step))


@app.post("/api/v1/settings")
async def api_settings(request: Request) -> JSONResponse:
    body = await request.json()
    kw = {k: body[k] for k in ("order_no", "population", "customer") if k in body}
    state.write_settings(**kw)
    return JSONResponse({"ok": True, "settings": state.read_settings()})


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
            "order_no": rec.get("order_no"),
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


@app.get("/api/v1/healthz")
def healthz() -> JSONResponse:
    try:
        with db.get_pool().connection() as conn:
            conn.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok", "db": "ok"})
    except Exception:
        return JSONResponse({"status": "fail", "db": "fail"}, status_code=503)
