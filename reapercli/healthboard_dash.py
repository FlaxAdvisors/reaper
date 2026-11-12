"""Grafana 11 dashboards for the site health board (spec 2026-11-09-site-health-board-design,
layout B "status board"): site-<site> plus the bang and switch drill-downs. They read the
recording rules reapercli/healthboard.py generates, from the bang's one local Prometheus
datasource (uid "prometheus")."""
DS = {"type": "prometheus", "uid": "prometheus"}
GREY, GREEN, AMBER, RED = "#5a5d63", "green", "#f2cc0c", "red"


def _target(expr, legend="", instant=False, fmt=None):
    t = {"datasource": DS, "expr": expr, "refId": "A", "legendFormat": legend}
    if instant:
        t["instant"], t["range"] = True, False
    if fmt:
        t["format"] = fmt
    return t


def _map(*pairs):
    return [{"type": "value", "options": {str(v): {"text": t, "color": c, "index": i}
                                           for i, (v, t, c) in enumerate(pairs)}}]


HEALTH_MAP = _map((-1, "no data", GREY), (0, "UP", GREEN), (1, "WARN", AMBER), (2, "DOWN", RED))
ITEM_MAP = _map((0, "ok", GREEN), (1, "n/a", GREY), (2, "standby", GREY), (3, "down", RED))
LINK_MAP = _map((-1, "no data", GREY), (0, "down", RED), (1, "up", GREEN), (2, "down (expected)", GREY),
                (3, "not live", GREY))


class _Grid:
    def __init__(self):
        self.y, self.panels, self.id = 0, [], 0

    def row(self, title):
        self.id += 1
        self.panels.append({"type": "row", "title": title, "id": self.id, "collapsed": False,
                            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []})
        self.y += 1

    def add(self, panels, w, h):
        x = 0
        for p in panels:
            if x + w > 24:
                x, self.y = 0, self.y + h
            self.id += 1
            p.update(id=self.id, gridPos={"x": x, "y": self.y, "w": w, "h": h}, datasource=DS)
            self.panels.append(p)
            x += w
        self.y += h


def _stat(title, expr, mappings, link=None, desc=""):
    p = {"type": "stat", "title": title, "description": desc, "targets": [_target(expr, instant=True)],
         "fieldConfig": {"defaults": {"mappings": mappings, "color": {"mode": "thresholds"},
                                      "thresholds": {"mode": "absolute", "steps": [{"color": GREY, "value": None}]}},
                         "overrides": []},
         "options": {"colorMode": "background", "graphMode": "none", "textMode": "value",
                     "reduceOptions": {"calcs": ["lastNotNull"], "values": False}}}
    if link:
        p["links"] = [{"title": "details", "url": link}]
    return p


def _gauge(title, expr, legend, steps=(70, 90), unit="percent", maxv=100):
    return {"type": "gauge", "title": title, "targets": [_target(expr, legend, instant=True)],
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "max": maxv, "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute", "steps": [
                                             {"color": GREEN, "value": None}, {"color": AMBER, "value": steps[0]},
                                             {"color": RED, "value": steps[1]}]}},
                            "overrides": []},
            "options": {"showThresholdMarkers": True, "reduceOptions": {"calcs": ["lastNotNull"], "values": False}}}


def _table(title, expr, keep, mapped_field, mappings):
    excl = {k: True for k in ("Time", "__name__", "job", "instance")}
    return {"type": "table", "title": title, "targets": [_target(expr, instant=True, fmt="table")],
            "transformations": [{"id": "organize", "options": {
                "excludeByName": excl, "indexByName": {k: i for i, k in enumerate(keep)},
                "renameByName": {"Value": mapped_field}}},
                {"id": "filterFieldsByName", "options": {"include": {"names": keep[:-1] + [mapped_field]}}}],
            "fieldConfig": {"defaults": {}, "overrides": [{
                "matcher": {"id": "byName", "options": mapped_field},
                "properties": [{"id": "mappings", "value": mappings},
                               {"id": "custom.cellOptions", "value": {"type": "color-background"}}]}]},
            "options": {"showHeader": True}}


def _dash(uid, title, panels, variables=()):
    return {"uid": uid, "title": title, "tags": ["reaper", "health"], "timezone": "browser",
            "schemaVersion": 39, "editable": False, "refresh": "30s",
            "time": {"from": "now-6h", "to": "now"}, "panels": panels,
            "templating": {"list": list(variables)}}


def _site_tile(name: str, link: str, here: bool) -> dict:
    """One federated-summary tile: a site's three counts, green at 0, linking to its board."""
    p = _stat(f"{name} (this site)" if here else name, "", [], link,
              "unhealthy devices · links down · services down; click for the site's board")
    p["targets"] = [dict(_target(f'site:{m}{{site="{name}"}}', legend, instant=True), refId=r)
                    for r, (m, legend) in zip("ABC", (("devices_unhealthy", "devices"), ("links_down", "links"),
                                                      ("services_down", "services")))]
    p["fieldConfig"]["defaults"]["thresholds"]["steps"] = [{"color": GREEN, "value": None},
                                                           {"color": RED, "value": 1}]
    p["options"]["textMode"] = "value_and_name"
    p["links"][0]["title"] = f"{name} board"
    return p


def site_dashboard(model, comps, sh, site: str, boards=None, domain: str = "") -> dict:
    """boards: {site: primary bang} for every active site (the summary row); default this one.
    domain: the lab domain (bang_domain, from the site vault) for links to the other sites' boards."""
    if boards and any(s != site for s in boards) and not domain:
        raise ValueError("site_dashboard: links to other sites need the lab domain (--domain)")
    S = f'site="{site}"'
    g = _Grid()
    g.row("Sites (federated summary)")
    boards = boards or {site: sh.primary}
    g.add([_site_tile(s, f"/d/site-{s}" if s == site else f"https://health.{b}.{domain}/d/site-{s}",
                      s == site) for s, b in boards.items()], 24 // max(len(boards), 1), 4)
    g.row("Devices")
    tiles = []
    for d in model.devices:
        drill = (f"/d/bang?var-bang={d.name}" if d.kind == "bang"
                 else f"/d/switch?var-switch={d.name}" if d.kind == "switch" else None)
        state = "" if d.state == "live" else f" · {d.state}"
        tiles.append(_stat(f"{d.name}{state}", f'reaper:device_health{{{S},device="{d.name}"}}', HEALTH_MAP,
                           drill, d.note))
        if d.bmc_ip:
            tiles.append(_stat(f"{d.name} BMC", f'reaper:device_health{{{S},device="{d.name}-bmc"}}', HEALTH_MAP,
                               f"/d/bang?var-bang={d.name}", f"{d.bmc_ip}"))
    g.add(tiles, 3, 4)
    g.row("Links and services")
    g.add([_table("Links", f'reaper:link_display{{{S}}}', ["a", "b", "speed", "expect", "note", "state"],
                  "state", LINK_MAP),
           _table("Services, containers and ports",
                  f'reaper:service_state{{{S}}} * on (site, component) group_left (scope, ports) '
                  f'reaper:component_info{{{S}}}',
                  ["component", "bang", "scope", "ports", "state"], "state", ITEM_MAP)], 12, 12)
    links = {}
    for l in model.links:
        for e in (l.a, l.b):
            dev = model.device(e.device)
            if dev and dev.kind == "bang":
                links.setdefault(e.device, []).append(e.port)
    for d in model.devices:
        if d.kind != "bang" or d.state != "live":
            continue
        b = f'{S},bang="{d.name}"'
        g.row(f"Resources · {d.name}")
        fs = (f'{{{b},fstype!~"tmpfs|overlay|squashfs|iso9660|devtmpfs|ramfs"}}')
        gauges = [
            _gauge(f"{d.name} · disk", f"max by (device) (100 * (1 - node_filesystem_avail_bytes{fs} / "
                                       f"node_filesystem_size_bytes{fs}))", "{{device}}"),
            _gauge(f"{d.name} · memory used", f"100 * (1 - node_memory_MemAvailable_bytes{{{b}}} / "
                                              f"node_memory_MemTotal_bytes{{{b}}})", ""),
            _gauge(f"{d.name} · memory pressure (major faults + swap-ins /s)",
                   f"rate(node_vmstat_pgmajfault{{{b}}}[5m]) + rate(node_vmstat_pswpin{{{b}}}[5m])", "",
                   steps=(100, 1000), unit="short", maxv=2000),
        ]
        for port in links.get(d.name, []):
            n = f'{b},device="{port}"'
            gauges.append(_gauge(f"{d.name} · {port}",      # the busier direction
                                 f'100 * max(label_replace(rate(node_network_receive_bytes_total{{{n}}}[5m]), '
                                 f'"dir", "rx", "", "") or label_replace(rate(node_network_transmit_bytes_total'
                                 f'{{{n}}}[5m]), "dir", "tx", "", "")) / max(node_network_speed_bytes{{{n}}})', ""))
        gauges.append(_gauge(f"{d.name} · load / cores",
                             f'100 * node_load5{{{b}}} / scalar(count(node_cpu_seconds_total{{{b},mode="idle"}}))', ""))
        g.add(gauges, 3, 5)
    return _dash(f"site-{site}", f"Site health · {site}", g.panels)


def _var(name, query):
    return {"name": name, "type": "query", "datasource": DS, "query": {"query": query, "refId": "V"},
            "refresh": 1, "includeAll": False, "multi": False}


def bang_dashboard() -> dict:
    B = 'bang="$bang"'
    g = _Grid()
    g.row("Health")
    g.add([_stat("bang", 'reaper:device_health{device="$bang"}', HEALTH_MAP),
           _stat("BMC", 'reaper:device_health{device="$bang-bmc"}', HEALTH_MAP),
           _stat("new SEL entries", f"reaper:sel_new{{{B}}}", []),
           _stat("SEL entries", f"max(reaper_sel_entries{{{B}}})", [])], 6, 4)
    g.add([_table("DIMMs (dimmerr)", f"reaper_dimm_ce{{{B}}} + on (slot, serial) group_left reaper_dimm_ue{{{B}}} * 1000",
                  ["slot", "serial", "ce+1000*ue"], "ce+1000*ue", []),
           _table("SMART per disk", f"smartctl_device_smart_status{{{B}}}", ["device", "status"], "status",
                  _map((1, "passed", GREEN), (0, "FAILED", RED))),
           _table("IPMI sensors not ok", f"reaper_ipmi_sensor_ok{{{B}}} == 0", ["sensor", "ok"], "ok",
                  _map((0, "not ok", RED)))], 8, 10)
    g.row("What runs")
    g.add([_table("Services", f"reaper:item_state{{{B}}}", ["component", "kind", "name", "state"], "state", ITEM_MAP),
           _table("Containers", f"reaper_container_running{{{B}}}", ["name", "running"], "running",
                  _map((1, "running", GREEN), (0, "not running", RED))),
           _table("Listening ports", f"reaper_listening{{{B}}}", ["proto", "port", "up"], "up", [])], 8, 12)
    return _dash("bang", "Bang · $bang", g.panels,
                 [_var("bang", 'label_values(reaper:device_health{kind="bang"}, device)')])


def switch_dashboard() -> dict:
    W = 'switch="$switch"'
    g = _Grid()
    g.row("Health")
    g.add([_stat("switch", 'reaper:device_health{device="$switch"}', HEALTH_MAP)], 6, 4)
    g.add([_table("Ports (ifOperStatus)", f"ifOperStatus{{{W}}}", ["ifName", "oper"], "oper",
                  _map((1, "up", GREEN), (2, "down", GREY), (7, "lowerLayerDown", GREY))),
           _table("Errors in (/s)", f"rate(ifInErrors{{{W}}}[5m]) > 0", ["ifName", "rate"], "rate", []),
           _table("Discards in (/s)", f"rate(ifInDiscards{{{W}}}[5m]) > 0", ["ifName", "rate"], "rate", [])], 8, 16)
    return _dash("switch", "Switch · $switch", g.panels,
                 [_var("switch", 'label_values(reaper:device_health{kind="switch"}, device)')])
