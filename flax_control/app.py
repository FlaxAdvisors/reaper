"""flax-control — unified UI + JSON API backed by Postgres."""
import datetime
from pathlib import Path

from fastapi import Body, FastAPI, Form, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .version import __version__
from . import records_browser_view, records_view, ownership_view
from . import dashboard_view  # noqa: E402

BASE_DIR = Path(__file__).parent

app = FastAPI(title="flax-control", version=__version__)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _ctx(**extra) -> dict:
    """Shared template context: version, db_status placeholder."""
    return {"version": __version__, "db_status": "ok", **extra}


# The fixed set of pipeline consumers we expect to ack the consumer_acks ledger.
# Drives the Pipeline tile denominator (always /5) + the Services page roster,
# so a never-acked fleet reads 0/5 rather than 0/0.
EXPECTED_CONSUMERS = ["flax-switch-sense", "flax-observe", "flax-discover",
                      "flax-classify", "flax-reconcile"]
# A healthy service acks at least this often. Covers the slowest cycle + margin.
ACK_FRESH_SECS = 120
# Per-consumer freshness overrides for services whose idle cadence is slower
# than the default window. flax-reconcile only acks when DHCP/desired_port
# activity occurs; when the lab is idle it sweeps every ~900s, so a 120s window
# would flag a healthy-but-idle reconcile as stale.
ACK_FRESH_SECS_BY_CONSUMER = {"flax-reconcile": 1080}  # idle sweep ~900s + margin
# Clean (non-failure) actions a healthy ack may carry.
_OK_ACTIONS = {"applied", "noop", "deferred"}
# Failure actions that mark a consumer failed regardless of freshness.
_FAILED_ACTIONS = {"failed", "skipped"}


def _now_iso() -> str:
    """Current UTC time as an ISO string. Wrapped so tests can monkeypatch it
    (consumer_health takes now_iso explicitly; the routes default to this)."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_iso(value) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z'). Returns an
    aware UTC datetime, or None if unparseable/empty."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _ack_newer(a, b) -> bool:
    """True if ack `a` is newer than `b` by consumed_at. A parseable timestamp
    always beats an unparseable/None one; otherwise the larger timestamp wins."""
    a_dt = _parse_iso(a.get("consumed_at"))
    b_dt = _parse_iso(b.get("consumed_at"))
    if b_dt is None:
        return a_dt is not None
    return a_dt is not None and a_dt > b_dt


def _source_state(row, consumer, now) -> str:
    """State of one (consumer, source) ack row: healthy / failed / stale."""
    action = row.get("action")
    if action in _FAILED_ACTIONS:
        return "failed"
    if action in _OK_ACTIONS:
        dt = _parse_iso(row.get("consumed_at"))
        fresh_secs = ACK_FRESH_SECS_BY_CONSUMER.get(consumer, ACK_FRESH_SECS)
        if dt is not None and (now - dt).total_seconds() <= fresh_secs:
            return "healthy"
        return "stale"
    return "stale"  # unknown action — not a clean ack


def consumer_health(acks, now_iso=None):
    """Per-expected-consumer health over the fixed 5, aggregating that
    consumer's sources (one row per (consumer, source)).

    Returns a list (in EXPECTED_CONSUMERS order) of
        {consumer, state, source, generation, consumed_at, detail}
    where state is one of:
      healthy  — no source failed and the newest source is a fresh clean ack
                 (action in {applied,noop,deferred} within the consumer's
                 freshness window: ACK_FRESH_SECS or a per-consumer override).
      failed   — every source's latest ack is in {failed,skipped}.
      degraded — some but not all sources failed (a partial outage). The row
                 carries the failing source's detail so it names what broke.
      stale    — no source failed but the newest source is too old / unparseable.
      missing  — no row for this consumer.

    The per-source split is what makes `degraded` observable: flax-switch-sense
    fans out one ack row per switch (source ``switches/<name>``), so one failing
    switch is no longer masked by a healthy switch's newer ``applied`` ack (a
    single shared ``switches`` row was last-write-wins and hid partial outages).

    `now_iso` is injectable for deterministic tests; defaults to _now_iso().
    """
    now = _parse_iso(now_iso) or datetime.datetime.now(datetime.timezone.utc)

    # Latest ack per (consumer, source).
    latest_cs: dict[tuple, dict] = {}
    for a in acks or []:
        c = a.get("consumer")
        if c is None:
            continue
        key = (c, a.get("source"))
        prev = latest_cs.get(key)
        if prev is None or _ack_newer(a, prev):
            latest_cs[key] = a

    # Group each consumer's per-source rows together.
    by_consumer: dict[str, list] = {}
    for (c, _src), row in latest_cs.items():
        by_consumer.setdefault(c, []).append(row)

    def _latest(rows):
        best = rows[0]
        for r in rows[1:]:
            if _ack_newer(r, best):
                best = r
        return best

    out = []
    for consumer in EXPECTED_CONSUMERS:
        rows = by_consumer.get(consumer)
        if not rows:
            out.append({"consumer": consumer, "state": "missing",
                        "source": None, "generation": None,
                        "consumed_at": None, "detail": None})
            continue
        failed_rows = [r for r in rows
                       if _source_state(r, consumer, now) == "failed"]
        if failed_rows:
            # Surface the failing source (its detail names what broke). A full
            # failure is every source down; a partial one is degraded.
            rep = _latest(failed_rows)
            state = "failed" if len(failed_rows) == len(rows) else "degraded"
        else:
            # No failures: newest source wins (healthy or stale), preserving the
            # single-source / latest-row behavior.
            rep = _latest(rows)
            state = _source_state(rep, consumer, now)
        out.append({"consumer": consumer, "state": state,
                    "source": rep.get("source"),
                    "generation": rep.get("generation"),
                    "consumed_at": rep.get("consumed_at"),
                    "detail": rep.get("detail")})
    return out


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe. Does NOT check Postgres — see /api/v1/healthz for that."""
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    switches_rows = queries.switches()
    acks = queries.consumer_acks()
    recent = queries.events(limit=25)
    health = consumer_health(acks, now_iso=_now_iso())
    by = {h["consumer"]: h["state"] for h in health}
    lease_model = leases_view.build_leases(queries.leases(), queries.reservations())
    return templates.TemplateResponse(request, "index.html", _ctx(
        pipeline=dashboard_view.build_pipeline(health),
        role_lanes=dashboard_view.build_role_lanes(queries.desired_by_role_kind()),
        healthy_count=sum(1 for h in health if h["state"] == "healthy"),
        expected_count=len(EXPECTED_CONSUMERS),
        switch_count=len(switches_rows),
        reachable_count=sum(1 for s in switches_rows if s["reachable"]),
        observe_port_count=len(queries.observe_state_all()),
        unreserved_leases=lease_model["unreserved_count"],
        classify_state=by.get("flax-classify", "missing"),
        reconcile_state=by.get("flax-reconcile", "missing"),
        recent_events=recent,
    ))


@app.get("/partials/pipeline", response_class=HTMLResponse)
def partial_pipeline(request: Request) -> HTMLResponse:
    health = consumer_health(queries.consumer_acks(), now_iso=_now_iso())
    return templates.TemplateResponse(request, "partials/pipeline.html",
                                       {"request": request,
                                        "pipeline": dashboard_view.build_pipeline(health)})


@app.get("/partials/events", response_class=HTMLResponse)
def partial_events(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/events.html",
                                       {"request": request,
                                        "recent_events": queries.events(limit=25)})


@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "devices.html",
                                       _ctx(devices=queries.devices()))


@app.get("/switches", response_class=HTMLResponse)
def switches_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "switches.html",
                                       _ctx(switches=queries.switches()))


from . import db  # noqa: E402  — placed here intentionally; app + db avoid circular import
from . import queries  # noqa: E402
from . import ll  # noqa: E402
from . import switches_view  # noqa: E402
from . import leases_view  # noqa: E402


@app.get("/switches/{switch}", response_class=HTMLResponse)
def switch_overview_page(request: Request, switch: str) -> HTMLResponse:
    """Per-switch overview: all ports for the switch."""
    all_ports = queries.ports_for_switch(switch)
    port_rows = switches_view.build_port_rows(
        all_ports, queries.desired_ports_for_switch(switch))
    all_switches = queries.switches()
    switch_row = next((s for s in all_switches if s["switch"] == switch), None)
    return templates.TemplateResponse(request, "switch_overview.html",
                                       _ctx(switch=switch, ports=port_rows,
                                            switch_row=switch_row))


@app.get("/switches/{switch}/{port:path}", response_class=HTMLResponse)
def switch_detail_page(request: Request, switch: str, port: str) -> HTMLResponse:
    """Per-port detail page — switch_facts + observe_state side by side."""
    # switch_facts ports are keyed by Arista canonical (Ethernet6/1);
    # observe_state by internal (et6b1). Look up both.
    all_ports = queries.ports_for_switch(switch)
    port_fact = next((p for p in all_ports if p["port"] == port), None)
    observe_row = queries.observe_state_one(switch, triage_compat.internal_port(port))
    actions = queries.reconcile_actions_for_port(switch, port)
    # LIVE lease-vs-reservation status (current truth) so the historical
    # actions table doesn't read as a live fault. Defaults to the empty shape
    # if the helper returns nothing, so the route never 500s.
    reconcile_status = queries.reconcile_status_for_port(switch, port) or {
        "converged": True, "live_mismatches": 0, "macs": []}
    observe_vars_sorted = None
    if observe_row and observe_row.get("vars"):
        observe_vars_sorted = sorted(
            observe_row["vars"].items(),
            key=lambda kv: kv[1].get("since") or "", reverse=True)
    return templates.TemplateResponse(request, "switch_detail.html",
                                       _ctx(switch=switch, port=port,
                                            port_fact=port_fact,
                                            observe_row=observe_row,
                                            observe_vars_sorted=observe_vars_sorted,
                                            reconcile_status=reconcile_status,
                                            actions=actions))


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request,
                 service: str | None = None,
                 kind: str | None = None,
                 mac: str | None = None,
                 switch: str | None = None,
                 port: str | None = None,
                 since: str | None = None,
                 limit: int = 200) -> HTMLResponse:
    rows = queries.events(service=service, kind=kind, mac=mac,
                           switch=switch, port=port,
                           since=since, limit=limit)
    return templates.TemplateResponse(request, "events.html",
                                       _ctx(events=rows,
                                            facets=queries.events_facets(),
                                            filters={"service": service, "kind": kind,
                                                     "switch": switch, "port": port,
                                                     "limit": limit}))


@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request) -> HTMLResponse:
    acks = queries.consumer_acks()
    health = consumer_health(acks, now_iso=_now_iso())
    return templates.TemplateResponse(request, "services.html",
                                       _ctx(acks=acks, consumer_health=health))


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request) -> HTMLResponse:
    cfg_dir = _os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")
    files = config_map_view.catalogue_with_mtimes(cfg_dir, _stat_mtime)
    config_map = config_map_view.build_config_map(
        files, queries.consumer_acks(), now_ts=__import__("time").time())
    return templates.TemplateResponse(request, "config.html", _ctx(
        config_map=config_map,
        reconcile_tunables=config_view.reconcile_tunables(),
        topology_files=config_view.topology_files(),
        classifier_dirs=config_view.classifier_dirs(),
        site_env=config_view.site_env_switches(),
        credentials=config_view.credentials_reference(),
        bmc_fw_manifest=config_view.bmc_fw_manifest(),
    ))


@app.get("/roles", response_class=HTMLResponse)
def roles_page(request: Request) -> HTMLResponse:
    """Read-only role-registry view: published roles.d definitions (name,
    generation, universe summary, capabilities/policy/record_keys) plus a
    live coverage check -- known switch_facts ports matched by no explicit
    claim (i.e. would resolve via catch_all only). DB-read only: roles.d
    itself is never mounted into this container."""
    registry = roles_view.build_registry(queries.roles(), queries.role_universe())
    uncovered = roles_view.coverage(queries.role_universe(), queries.switch_ports_all())
    return templates.TemplateResponse(request, "roles.html", _ctx(
        registry=registry, uncovered=uncovered))


@app.get("/shadow", response_class=HTMLResponse)
def shadow_page(request: Request) -> HTMLResponse:
    """Read-only shadow-materializer view: latest-run convergence banner
    (materializer_plan), recent plan history, mac ownership events, and a
    per-owner desired_reservations summary. DB-read only -- phase 2's shadow
    materializer (flax_classify.materializer) is the sole writer of these
    tables; this page never writes anything."""
    shadow = shadow_view.build_shadow(
        queries.materializer_recent(),
        queries.ownership_events_recent(),
        queries.desired_summary(),
    )
    return templates.TemplateResponse(request, "shadow.html", _ctx(shadow=shadow))


from . import triage_view  # noqa: E402
from . import bmcfw_view  # noqa: E402


@app.get("/bmc-fw-triage", response_class=HTMLResponse)
def bmc_fw_triage_page(request: Request) -> HTMLResponse:
    """Read-only BMC-firmware fleet: one row per Tioga Pass DUT port with its
    current->target version and update state (joined from the manifest,
    observe_state, and the bmc_fw worker store). Renamed from /bmc-fw
    (Task 5) — /bmc-fw now redirects here; see bmc_fw_redirect below."""
    matcher = bmcfw_view.load_matcher()
    store = bmcfw_view.read_store()
    rows = bmcfw_view.fleet_rows(queries.observe_state_all(), store, matcher)
    now = datetime.datetime.now().timestamp()
    store_updated = bmcfw_view.store_last_updated(store)
    return templates.TemplateResponse(request, "bmcfw.html", _ctx(
        rows=rows, targets=matcher.targets, scope="triage",
        store_updated=store_updated,
        updated_str=bmcfw_view.fmt_updated(store_updated, now),
        now_str=bmcfw_view.fmt_ts(now)))


@app.get("/bmc-fw")
def bmc_fw_redirect():
    """Routes are frozen for external consumers: keep /bmc-fw live as a
    redirect rather than 404 it now that the page moved to /bmc-fw-triage."""
    return RedirectResponse(url="/bmc-fw-triage", status_code=303)


from . import post_bmcfw_view  # noqa: E402


@app.get("/bmc-fw-post", response_class=HTMLResponse)
def bmc_fw_post_page(request: Request) -> HTMLResponse:
    """Read-only post BMC/NIC/BIOS firmware fleet: one row per port, merged
    from the post fw file stores (post_fw.json/post_nic_fw.json/
    post_bios_fw.json — sole-written by flax_post/fwd, mounted read-only).
    No DB read, no writes -- flashing stays in flax_post/fwd; this is a
    documented lane deviation (see docs/flax-storage-delta.md)."""
    bmc, nic, bios = post_bmcfw_view.read_stores()
    rows = post_bmcfw_view.fleet_rows(bmc, nic, bios)
    # Precompute each row's phase->badge-color kind here so the phase->color
    # mapping lives in ONE place (post_bmcfw_view._PHASE_BADGE via badge_for)
    # instead of being re-declared inline in the template.
    for r in rows:
        r["bmc_badge"] = post_bmcfw_view.badge_for(r["bmc"].get("phase"))
        r["nic_badge"] = post_bmcfw_view.badge_for(r["nic"].get("phase"))
        r["bios_badge"] = post_bmcfw_view.badge_for(r["bios"].get("phase"))
    now = datetime.datetime.now().timestamp()
    store_updated = post_bmcfw_view.last_updated(bmc, nic, bios)
    return templates.TemplateResponse(request, "bmcfw_post.html", _ctx(
        rows=rows, scope="post",
        store_updated=store_updated,
        updated_str=bmcfw_view.fmt_updated(store_updated, now),
        now_str=bmcfw_view.fmt_ts(now)))


@app.get("/triage", response_class=HTMLResponse)
def triage_page(request: Request) -> HTMLResponse:
    """Triage rack summary: one row per geometry.json (triage) port with the 12
    observe state vars + timestamps — the new-UI take on the old switchportrecon
    10988 dashboard. Only geometry ports (turtle/post ports are excluded)."""
    rows = triage_view.build_rows(_load_geometry(), queries.observe_state_all())
    return templates.TemplateResponse(request, "triage.html", _ctx(
        rows=rows, var_order=list(triage_view.VAR_ORDER)))


@app.get("/reservations", response_class=HTMLResponse)
def reservations_page(request: Request) -> HTMLResponse:
    """Shadow-mode view of classify_proposals (flax-classify output).
    No DHCP server reads this table yet — Plan 5 cuts over."""
    return templates.TemplateResponse(request, "reservations.html",
                                       _ctx(rows=queries.reservations()))


@app.get("/leases", response_class=HTMLResponse)
def leases_page(request: Request) -> HTMLResponse:
    """Active Kea leases (kea.lease4/lease6, state=0) joined against
    reservations (desired) — highlights unreserved/stale leases."""
    model = leases_view.build_leases(queries.leases(), queries.reservations())
    # Per-subnet count summary (utilization-ish — we don't know pool sizes here,
    # so we just show active-lease counts per subnet).
    by_subnet: dict[Any, int] = {}
    for r in model["rows"]:
        by_subnet[r["subnet_id"]] = by_subnet.get(r["subnet_id"], 0) + 1
    subnet_summary = sorted(by_subnet.items(), key=lambda kv: (kv[0] is None, kv[0]))
    return templates.TemplateResponse(request, "leases.html",
                                       _ctx(model=model, leases=model["rows"],
                                            subnet_summary=subnet_summary))


@app.get("/api/v1/switches")
def api_switches() -> JSONResponse:
    return JSONResponse(queries.switches())


@app.get("/api/v1/switches/{switch}/ports")
def api_switch_ports(switch: str) -> JSONResponse:
    return JSONResponse(queries.ports_for_switch(switch))


@app.get("/api/v1/healthz")
def api_healthz() -> JSONResponse:
    """Postgres-aware health check used by keepalived chk_services."""
    try:
        pool = db.get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                if row != (1,):
                    raise RuntimeError(f"unexpected SELECT 1 result: {row!r}")
        return JSONResponse({"status": "ok", "db": "ok"})
    except Exception as e:
        return JSONResponse(
            {"status": "fail", "db": "fail", "error": str(e)},
            status_code=503,
        )


@app.get("/api/v1/observe/state")
def api_observe_state() -> JSONResponse:
    return JSONResponse(queries.observe_state_all())


@app.get("/api/v1/observe/state/{switch}/{port:path}")
def api_observe_state_one(switch: str, port: str) -> JSONResponse:
    """Single-port observe_state. Uses `port:path` so slashes in port names
    (e.g. Ethernet6/1) are preserved."""
    row = queries.observe_state_one(switch, port)
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.get("/api/v1/events")
def api_events(
    service: str | None = None,
    kind: str | None = None,
    mac: str | None = None,
    switch: str | None = None,
    port: str | None = None,
    since: str | None = None,
    limit: int = 200,
) -> JSONResponse:
    """Audit log query. All filters optional; combined via AND.
    Default limit 200; max 5000 (enforced inside queries.events)."""
    rows = queries.events(service=service, kind=kind, mac=mac,
                           switch=switch, port=port,
                           since=since, limit=limit)
    return JSONResponse(rows)


@app.get("/api/v1/acks")
def api_acks() -> JSONResponse:
    return JSONResponse(queries.consumer_acks())


@app.get("/api/v1/reservations")
def api_reservations() -> JSONResponse:
    return JSONResponse(queries.reservations())


@app.get("/records", response_class=HTMLResponse)
def records_page(request: Request, q: str | None = None,
                 dut: int | None = None, order: str | None = None,
                 customer: str | None = None, kind: str | None = None,
                 since: str | None = None) -> HTMLResponse:
    """Phase-5 work-record browser: smart search / DUT biography / key slice."""
    search = bio = slice_model = None
    if dut is not None:
        d = records_view.dut_by_id(dut)
        if d is not None:
            assemblies = records_view.lookup_duts(mac=d["p0_mac"], assembly="all")
            rows = records_view.dut_records(dut, kind=kind, since=since, limit=500)
            bio = records_browser_view.build_biography(d, assemblies, rows)
    elif order:
        slice_model = records_browser_view.build_slice(
            "order", order, records_view.records_by_key("order", order, limit=500))
    elif customer:
        slice_model = records_browser_view.build_slice(
            "customer", customer,
            records_view.records_by_key("customer", customer, limit=500))
    elif q:
        kind_term, norm = records_view.detect_term(q)
        mac_duts = id_dut = serial_duts = order_duts = customer_duts = None
        if kind_term == "mac":
            mac_duts = records_view.lookup_duts(mac=norm, assembly="all")
        elif kind_term == "dut_id":
            id_dut = records_view.dut_by_id(norm)
        if kind_term == "text" or (kind_term == "dut_id" and id_dut is None):
            # numeric misses fall through: a serial/order can be all digits
            text = str(norm) if kind_term == "dut_id" else norm
            serial_duts = records_view.lookup_duts(serial=text, assembly="all")
            order_duts = records_view.duts_for_key("order", text)
            customer_duts = records_view.duts_for_key("customer", text)
        search = records_browser_view.build_search(
            q, mac_duts=mac_duts, id_dut=id_dut, serial_duts=serial_duts,
            order_duts=order_duts, customer_duts=customer_duts)
    return templates.TemplateResponse(request, "records.html", _ctx(
        q=q, kind=kind, search=search, bio=bio, slice=slice_model))


@app.get("/ownership", response_class=HTMLResponse)
def ownership_page(request: Request, mac: str | None = None) -> HTMLResponse:
    """Phase-5 roaming ledger: handoff history + rapid-roamer flagging."""
    warn = int(_os.environ.get("CONTROL_ROAM_WARN_24H", "4"))
    model = ownership_view.build_ownership(
        queries.ownership_events_recent(limit=500, mac=mac),
        queries.roaming_counts_24h(), warn)
    return templates.TemplateResponse(request, "ownership.html",
                                      _ctx(request=request, model=model, mac=mac))


@app.get("/lanes", response_class=HTMLResponse)
def lanes_page(request: Request) -> HTMLResponse:
    """Phase-5 per-role lane cards (core tables only)."""
    registry = roles_view.build_registry(queries.roles(), queries.role_universe())
    shadow_model = shadow_view.build_shadow(
        queries.materializer_recent(), queries.ownership_events_recent(),
        queries.desired_summary())
    now = datetime.datetime.now(datetime.timezone.utc)
    cards = lanes_view.build_lanes(
        registry, queries.desired_by_role_kind(), shadow_model,
        queries.work_record_counts(), queries.roaming_role_counts_24h(),
        lanes_view.parse_role_ui_links(_os.environ.get("CONTROL_ROLE_UI_LINKS", "")),
        now, int(_os.environ.get("CONTROL_MATERIALIZER_STALE_SECS", "180")))
    return templates.TemplateResponse(request, "lanes.html",
                                      _ctx(request=request, cards=cards))


@app.get("/layers/sensing", response_class=HTMLResponse)
def layers_sensing_page(request: Request) -> HTMLResponse:
    """Phase-5 layer health: 'are my eyes open?' — sensing-service acks,
    switch reachability, observe_state coverage, active-lease activity."""
    health = consumer_health(queries.consumer_acks())
    svcs = layers_view._LAYER_SERVICES["sensing"]
    model = layers_view.build_sensing(
        health, queries.switches(), queries.observe_state_stats(),
        queries.active_lease_count(),
        config_rows=_layer_config_rows(svcs),
        anomalies=queries.layer_anomalies(svcs),
        events=queries.events_for_services(svcs))
    return templates.TemplateResponse(request, "layer.html", _ctx(
        request=request, title="Sensing",
        question="Are my eyes open? Sensing turns the lab into tables.",
        model=model))


@app.get("/layers/policy", response_class=HTMLResponse)
def layers_policy_page(request: Request) -> HTMLResponse:
    """Phase-5 layer health: 'is the brain deciding, from fresh facts?' —
    classify ack, role registry, and the /roles coverage gap."""
    health = consumer_health(queries.consumer_acks())
    registry = roles_view.build_registry(queries.roles(), queries.role_universe())
    uncovered = roles_view.coverage(queries.role_universe(),
                                    queries.switch_ports_all())
    svcs = layers_view._LAYER_SERVICES["policy"]
    model = layers_view.build_policy(
        health, registry, queries.desired_summary(), len(uncovered),
        config_rows=_layer_config_rows(svcs),
        anomalies=queries.layer_anomalies(svcs),
        events=queries.events_for_services(svcs))
    return templates.TemplateResponse(request, "layer.html", _ctx(
        request=request, title="Policy",
        question="Is the brain deciding, and from fresh facts?",
        model=model))


@app.get("/layers/actuation", response_class=HTMLResponse)
def layers_actuation_page(request: Request) -> HTMLResponse:
    """Phase-5 layer health: 'are hands moving, and only as instructed?' —
    reconcile ack, materializer write-freeze detector, reconcile cadence,
    and active intentional-flap holds."""
    health = consumer_health(queries.consumer_acks())
    shadow_model = shadow_view.build_shadow(
        queries.materializer_recent(), queries.ownership_events_recent(),
        queries.desired_summary())
    now = datetime.datetime.now(datetime.timezone.utc)
    svcs = layers_view._LAYER_SERVICES["actuation"]
    model = layers_view.build_actuation(
        health, shadow_model, queries.kea_hosts_count(),
        queries.reconcile_cadence(), queries.intentional_flap_active(),
        now, int(_os.environ.get("CONTROL_MATERIALIZER_STALE_SECS", "180")),
        config_rows=_layer_config_rows(svcs),
        anomalies=queries.layer_anomalies(svcs),
        events=queries.events_for_services(svcs))
    return templates.TemplateResponse(request, "layer.html", _ctx(
        request=request, title="Actuation",
        question="Are hands moving, and only as instructed?",
        model=model))


@app.get("/api/records/duts")
def api_records_duts(mac: str | None = None, serial: str | None = None,
                     assembly: str = "current") -> JSONResponse:
    """Phase-4 DUT lookup: most-recent assembly by default (assembly=all for
    a component's earlier pairings)."""
    if not mac and not serial:
        return JSONResponse({"error": "mac or serial required"}, status_code=422)
    return JSONResponse(records_view.lookup_duts(mac=mac, serial=serial,
                                                 assembly=assembly))


@app.get("/api/records/{dut_id}")
def api_records_for_dut(dut_id: int, kind: str | None = None,
                        stage: str | None = None, since: str | None = None,
                        limit: int = 200) -> JSONResponse:
    """A DUT's append-only event log, newest-first."""
    return JSONResponse(records_view.dut_records(dut_id, kind=kind, stage=stage,
                                                 since=since, limit=limit))


@app.get("/api/records")
def api_records(order: str | None = None, customer: str | None = None,
                limit: int = 200) -> JSONResponse:
    """The post-engagement slice by the role's record_keys lenses."""
    if order:
        return JSONResponse(records_view.records_by_key("order", order,
                                                        limit=limit))
    if customer:
        return JSONResponse(records_view.records_by_key("customer", customer,
                                                        limit=limit))
    return JSONResponse({"error": "order or customer required"}, status_code=422)


from . import operator_notes  # noqa: E402


@app.patch("/api/v1/reservations/{mac_hex}/operator_note")
def api_patch_operator_note(mac_hex: str, body: dict = Body(...)):
    """Set or clear the operator_note on a kea.hosts reservation.

    Body: {"note": "free-form text"} -- empty string clears.
    """
    if "note" not in body:
        return JSONResponse(
            {"error": "body must contain a 'note' key"}, status_code=400)
    note = body["note"]
    try:
        operator_notes.update_operator_note(mac_hex, note)
    except operator_notes.NotFound:
        return JSONResponse(
            {"error": f"reservation not found: {mac_hex}"}, status_code=404)
    return JSONResponse({"mac": mac_hex, "operator_note": note})


@app.post("/reservations/{mac_hex}/operator_note")
def page_post_operator_note(mac_hex: str, note: str = Form("")):
    """Form-encoded POST -> calls update_operator_note. Redirects back to
    /reservations so the page reload shows the new note."""
    try:
        operator_notes.update_operator_note(mac_hex, note)
    except operator_notes.NotFound:
        return JSONResponse(
            {"error": f"reservation not found: {mac_hex}"}, status_code=404)
    return RedirectResponse(url="/reservations", status_code=303)


from . import reservations_delete  # noqa: E402


@app.post("/reservations/{mac_hex}/delete")
def page_post_delete_reservation(mac_hex: str):
    """Hard-delete an orphaned reservation (device gone -> classify won't
    re-create it). 303s back to /reservations. A still-live device's row would
    simply be re-created next classify cycle, so this only sticks for orphans."""
    try:
        reservations_delete.delete_reservation(mac_hex)
    except reservations_delete.NotFound:
        return JSONResponse(
            {"error": f"reservation not found: {mac_hex}"}, status_code=404)
    return RedirectResponse(url="/reservations", status_code=303)


from . import aliases as aliases_mod  # noqa: E402


@app.post("/reservations/{mac_hex}/aliases")
def page_post_aliases(mac_hex: str, aliases: str = Form("")):
    """Set DNS hostname aliases (space-separated) on a reservation; flax-classify
    renders them alongside the primary hostname in the dnsmasq hosts file. 303
    back. Invalid (non-DNS) tokens 400; unknown mac 404."""
    try:
        aliases_mod.update_aliases(mac_hex, aliases)
    except aliases_mod.NotFound:
        return JSONResponse(
            {"error": f"reservation not found: {mac_hex}"}, status_code=404)
    except aliases_mod.InvalidAlias as exc:
        return JSONResponse(
            {"error": f"invalid alias: {exc}"}, status_code=400)
    return RedirectResponse(url="/reservations", status_code=303)


import json as _json  # noqa: E402
import os as _os      # noqa: E402
import re as _re      # noqa: E402


_GEOMETRY_PATH = _os.environ.get("FLAX_GEOMETRY_PATH", "/etc/flax/geometry.json")
_geometry_cache: list[dict] | None = None


def _load_geometry() -> list[dict]:
    """Load /etc/flax/geometry.json once per process."""
    global _geometry_cache
    if _geometry_cache is None:
        try:
            with open(_GEOMETRY_PATH) as f:
                _geometry_cache = _json.load(f)
        except OSError:
            _geometry_cache = []
    return _geometry_cache


def _ou_for_port(port: str) -> str:
    """Look up OU for a port; '' if not in geometry."""
    for entry in _load_geometry():
        if entry.get("port") == port:
            return entry.get("ou", "")
    return ""


def _geometry_switch_for_port(port: str) -> str | None:
    """The switch geometry.json records for a rack port (the TRIAGE switch,
    e.g. rabbit-gouda). None if the port isn't a rack port. The
    switchportrecond-compat endpoints must scope observe_state lookups to this
    switch: observe_state is cross-role now (observe enrolls post switches too),
    so matching by bare port number would leak post rows onto triage tiles."""
    for entry in _load_geometry():
        if entry.get("port") == port:
            return entry.get("switch")
    return None


def _in_rack_geometry(port: str) -> bool:
    """True iff the port is in the rack geometry.json -- an Arista rabbit DUT
    port with a rack OU/column.

    The legacy triage rack UI consumes /api/v1/ports + /api/v1/port and models
    ONLY rack-positioned DUT ports. Turtle (Cumulus OOB-mgmt) ports live in
    turtle-geometry.json, NOT geometry.json, so they have no rack OU/column --
    the triage frontend parseInt()s the empty ou and renders NaN rows. Gate the
    switchportrecon-compat endpoints on rack-geometry membership so turtle ports
    (which belong to the DHCP/observe world, not the rack view) are excluded.
    """
    return any(entry.get("port") == port for entry in _load_geometry())


from . import reconcile_requests  # noqa: E402
from . import triage_compat   # noqa: E402
from . import config_view  # noqa: E402
from . import config_map_view  # noqa: E402
from . import roles_view  # noqa: E402
from . import shadow_view  # noqa: E402
from . import lanes_view  # noqa: E402
from . import layers_view  # noqa: E402


def _stat_mtime(path: str) -> float | None:
    """os.stat().st_mtime, or None if the file is absent/unreadable.

    Shared by config_page() and the /layers/* routes' config_map_view.
    catalogue_with_mtimes() call (injected so both are testable without
    touching the filesystem)."""
    try:
        return _os.stat(path).st_mtime
    except OSError:
        return None


def _layer_config_rows(svcs) -> list[dict]:
    """CATALOGUE files whose readers intersect this layer's services, run
    through build_config_map so each /layers/* page shows only the config
    that drives it (not the whole /config catalogue)."""
    cfg_dir = _os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")
    files = [f for f in config_map_view.catalogue_with_mtimes(cfg_dir, _stat_mtime)
             if set(f.get("readers", [])) & set(svcs)]
    return config_map_view.build_config_map(
        files, queries.consumer_acks(), now_ts=__import__("time").time())


@app.post("/reconcile/flap")
def post_flap(mac: str = Form(...), switch: str = Form(...), port: str = Form(...),
              kind: str = Form(""), return_to: str = Form("/")):
    """Operator-initiated flap: enqueue a reconcile_requests row, redirect back.
    Note: the actual switch flap runs in flax-reconcile on the MASTER.

    The device page's flap form passes devices.port in internal short form
    (et6b1); the switch_detail page passes the Arista URL form. flax-reconcile's
    flap + sentinel are Arista-canonical (spec §6), so canonicalize here.
    arista_port is idempotent, so applying it to the already-canonical
    switch_detail value is a no-op (no double-conversion)."""
    reconcile_requests.enqueue_flap(mac=mac, switch=switch,
                                    port=triage_compat.arista_port(port),
                                    kind=(kind or None), operator="ui")
    return RedirectResponse(url=return_to, status_code=303)


@app.post("/reconcile/bmc-reset")
def post_bmc_reset(mac: str = Form(...), switch: str = Form(...),
                   port: str = Form(...), kind: str = Form(""),
                   return_to: str = Form("/")):
    """Operator-initiated BMC reset (Redfish Manager.Reset ForceRestart):
    enqueue a reconcile_requests row with reason='operator bmc-reset', redirect
    back. The actual Redfish reset runs in flax-reconcile on the MASTER, which
    resolves the BMC IP from this mac's active Kea lease. Port is canonicalized
    to Arista (idempotent) to match the flap path's enqueue shape."""
    reconcile_requests.enqueue_bmc_reset(
        mac=mac, switch=switch, port=triage_compat.arista_port(port),
        kind=(kind or None), operator="ui")
    return RedirectResponse(url=return_to, status_code=303)


from . import omnibox  # noqa: E402
from . import dut_view  # noqa: E402


@app.get("/search")
def search_route(request: Request, q: str = ""):
    r = omnibox.resolve(
        q,
        device_lookup=lambda m: (m if queries.device_one(queries._colon_mac(m)) else None),
        port_lookup=queries.device_mac_by_switch_port,
        serial_lookup=queries.device_macs_by_serial,
        hostname_lookup=queries.hostname_macs,
    )
    if r["kind"] == "mac":
        return RedirectResponse(url=f"/devices/{r['mac']}", status_code=303)
    return templates.TemplateResponse(request, "search.html", _ctx(
        q=q, candidates=r.get("candidates", []), found=(r["kind"] != "none")))


@app.get("/devices/{mac}", response_class=HTMLResponse)
def device_detail_page(request: Request, mac: str) -> HTMLResponse:
    # Normalise the path arg to canonical colon-lowercase so either input form
    # resolves: hex-no-colons (legacy /devices/1c34da7f9d32 links) AND colon-form
    # (the new MAC-consistent links). devices.mac is stored colon-form, so we
    # canonicalise before lookups; the bare-hex form would otherwise miss.
    mac = queries._colon_mac(mac)
    full = queries.device_full(mac)
    # Keep backward-compat: device and actions stay in context.
    device = full  # device_detail.html reads device.mac / .switch / .port / .kind / .latched
    actions = (queries.reconcile_actions_for_port(
                   full["switch"], triage_compat.arista_port(full["port"]))
               if full else [])
    op_request = queries.reconcile_request_for_mac(mac)
    # Merged biography across single-writer tables (read-only).
    own_events = queries.ownership_events_recent(mac=mac, limit=100)
    duts = records_view.lookup_duts(mac=mac, assembly="all")
    work_records = []
    for d in duts:
        work_records.extend(records_view.dut_records(d["dut_id"], limit=200))
    biography = dut_view.build_biography(
        first_seen=(full or {}).get("last_seen"),
        observe=(full or {}).get("observe"),
        reconcile_actions=actions,
        ownership_events=own_events,
        work_records=work_records,
    )
    # Expose flat bmc_state for easy template branching.
    _obs_vars = (full or {}).get("observe") or {}
    _obs_vars = (_obs_vars.get("vars") or {}) if _obs_vars else {}
    bmc_state = {
        "bmcipmi": (_obs_vars.get("bmcipmi") or {}).get("value"),
        "bmcping": (_obs_vars.get("bmcping") or {}).get("value"),
    }
    # IPv6 link-local (EUI-64) for ssh/ping6 to the device on its access VLAN.
    # vid comes from the device's reservation classify block; parent iface is
    # eth0 (the bang host mgmt-side parent on braintree/eindhoven), overridable
    # via FLAX_LL_PARENT_IFACE. Best-effort: never let a bad MAC 500 the page.
    ipv6_ll = None
    if full:
        _vid = (((full.get("reservation") or {}).get("classify") or {}).get("vid"))
        _parent = _os.environ.get("FLAX_LL_PARENT_IFACE", "eth0")
        try:
            ipv6_ll = ll.ll_with_zone(full["mac"], _vid, parent=_parent)
        except (ValueError, KeyError):
            ipv6_ll = None
    # Role-context firmware link: a triage-geometry (switch, port) pair shows
    # the triage BMC FW fleet view, anything else (post ports, unknown) falls
    # to the post file-store view. This must match on the FULL (switch, port)
    # pair, not the bare port number: triage and post switches at a site can
    # reuse the same port tokens (e.g. eindhoven rabbit-gouda/et10b1 vs
    # rabbit-edam/et10b1), so a bare-port match would wrongly route a post
    # device to /bmc-fw-triage. Mirrors the /api/v1/port switch-scoped gate.
    fw_href = ("/bmc-fw-triage"
               if full and _geometry_switch_for_port(full["port"]) == full["switch"]
               else "/bmc-fw-post")
    return templates.TemplateResponse(request, "device_detail.html",
                                       _ctx(mac=mac, device=device, actions=actions,
                                            full=full, op_request=op_request,
                                            bmc_state=bmc_state, ipv6_ll=ipv6_ll,
                                            biography=biography, fw_href=fw_href))


@app.get("/api/v1/ports")
def api_ports() -> JSONResponse:
    state = queries.observe_state_all()
    # switchportrecond returns just the port tokens (sorted). Restrict to rack-
    # geometry ports by their FULL (switch, port) pair, not the bare port
    # number: observe_state is cross-role now, so a post switch sharing a rack
    # port number must not add that port to the triage list. Turtle OOB-mgmt
    # ports are excluded the same way (not in geometry).
    geom_pairs = set()
    geom_switchless_ports = set()
    for e in _load_geometry():
        if e.get("switch"):
            geom_pairs.add((e["switch"], e.get("port")))
        else:  # switch-less geometry entry -> legacy port-only membership
            geom_switchless_ports.add(e.get("port"))
    tokens = sorted(set(row["port"] for row in state.values()
                        if (row["switch"], row["port"]) in geom_pairs
                        or row["port"] in geom_switchless_ports))
    return JSONResponse(tokens)


@app.get("/api/v1/port/{port}")
def api_port(port: str) -> JSONResponse:
    """Single port's status.json (switchportrecond shape).

    Scoped to the TRIAGE (geometry) switch for this port. observe_state is
    cross-role now, so this must bind the geometry switch (rabbit-gouda) --
    matching the bare port number would return a post switch's row that shares
    the number (rabbit-edam), leaking post devices onto triage tiles.
    """
    # Turtle (Cumulus OOB-mgmt) ports are not in the rack geometry and are not
    # part of the legacy triage rack view -- 404 so triage never renders a NaN
    # row for them (mirrors the /api/v1/ports filter).
    if not _in_rack_geometry(port):
        return JSONResponse({"state": "missing"}, status_code=404)
    geom_switch = _geometry_switch_for_port(port)
    state = queries.observe_state_all()
    if geom_switch is not None:
        matching = [row for row in state.values()
                    if row["port"] == port and row["switch"] == geom_switch]
    else:
        # switch-less geometry entry (not the case for real geometry.json,
        # which always records the switch) -> legacy port-only match.
        matching = [row for row in state.values() if row["port"] == port]
    if not matching:
        return JSONResponse({"state": "missing"}, status_code=404)
    row = matching[0]
    ou = _ou_for_port(port)
    return JSONResponse(triage_compat.observe_row_to_triage_status(row, ou=ou))


_RACKNAME_PATH = _os.environ.get("FLAX_RACKNAME_PATH", "/etc/rackname")


def _read_rackname() -> str:
    """rackName for /api/v1/rackous. switchportrecond reads /etc/rackname
    (mounted from /etc/hostname). Same source here."""
    try:
        with open(_RACKNAME_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


@app.get("/api/v1/rackous")
def api_rackous() -> JSONResponse:
    geometry = _load_geometry()
    # switchportrecond shape: {ou: '20', port: 'Et6/1'} — strip column letter
    out_geom = []
    for entry in geometry:
        port = entry.get("port", "")
        ou_full = entry.get("ou", "")
        m = _re.match(r"^(\d+)([LCR])$", ou_full or "")
        ou_num = m.group(1) if m else ou_full
        out_geom.append({"ou": ou_num, "port": triage_compat.display_port(port)})
    return JSONResponse({
        "maxPower": 0,
        "geometry": out_geom,
        "rackName": _read_rackname(),
    })
