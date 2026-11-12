"""Site health board generator (spec 2026-11-09-site-health-board-design): from the site
model (files/configs/<site>/devices.yml) and reaper's components.yml, build what each
bang's Prometheus needs:

  expected/<bang>.prom          reaper_expected{component,kind,name,vip_gated,optional} 1
                                for every unit / container / tcp / udp port placed on <bang>
  rules/healthboard-<site>.yml  recording rules (device / link / service state) + alerts
  node-unit-include.txt         node_exporter's systemd unit-include regex

Codes. item/service state: 0 ok, 1 n/a (optional, not installed), 2 standby (VIP-gated
on the non-holder), 3 down; so max() is the worst. device health: -1 no data or not
live, 0 ok, 1 warn, 2 down. link state: -1 no data, 0 down, 1 up.

Every raw selector names site="<site>": federated copies of another site's series never
feed this site's rules, so a fault alerts once.
"""
import re
from pathlib import Path

import yaml

from reapercli.lib.components import load_components, placed_on
from reapercli.lib.inventory import site_hosts
from reapercli.lib.sitemodel import load_model
from reapercli.lib.sites import load_sites

# units the bangs run outside components.yml that the board still watches
BASE_UNITS = ("sshd", "tailscaled", "prometheus-node_exporter", "chronyd", "docker", "firewalld")
_ARISTA = (("Et", "Ethernet"), ("Ma", "Management"))


def _esc(s: str) -> str:
    return re.sub(r'(["\\])', r"\\\1", str(s))


def ifname(vendor: str, port: str) -> str:
    """The SNMP ifName of a port as devices.yml writes it (Arista short -> long form)."""
    if vendor == "arista":
        for short, long_ in _ARISTA:
            if re.fullmatch(short + r"[0-9][0-9/]*", port):
                return long_ + port[len(short):]
    return port


# ---------------------------------------------------------------- expectations
def expected_prom(model, comps, sh, site, root: Path, bang: str) -> str:
    sites = load_sites(Path(root))
    site_obj = sites[site]
    if bang in site_obj.dormant_hosts or (model.device(bang) and model.device(bang).state != "live"):
        return "# reaper_expected: this bang is not live, nothing expected\n"
    lines = ["# HELP reaper_expected What reaper deploys on this bang (components.yml + placement)",
             "# TYPE reaper_expected gauge"]
    for c in sorted(comps.values(), key=lambda c: c.name):
        if bang not in placed_on(c, sh, site_obj, Path(root)):
            continue
        # a unit with a timer of the same name is a oneshot: idle between runs, so its
        # TIMER is what must be active
        items = ([("unit", f"{u}.timer" if u in c.timers else f"{u}.service") for u in c.units]
                 + [("container", x) for x in c.containers]
                 + [("tcp", str(p)) for p in c.ports] + [("udp", str(p)) for p in c.udp_ports])
        for kind, name in items:
            lines.append(f'reaper_expected{{component="{c.name}",kind="{kind}",name="{_esc(name)}",'
                         f'vip_gated="{int(c.vip_gated)}",optional="{int(c.optional)}"}} 1')
    return "\n".join(lines) + "\n"


def unit_include_regex(comps) -> str:
    units = sorted({u for c in comps.values() for u in c.units} | set(BASE_UNITS))
    esc = [re.sub(r"([.+*?()\[\]{}|^$\\])", r"\\\1", u) for u in units]
    return "^(" + "|".join(esc + ["health-hook@.*"]) + r")\.(service|timer)$"


# ---------------------------------------------------------------- rules
def _device_expr(d, model, site: str) -> str:
    S = f'site="{site}"'
    if d.kind == "bang":
        b = f'{S},bang="{d.name}"'
        up = f'max(up{{job="node",{b}}})'
        warn = " or ".join([
            f"max(reaper_ipmi_kcs_ok{{{b}}}) == 0",
            f"count(reaper_ipmi_sensor_ok{{{b}}} == 0) > 0",
            f"sum(reaper_dimm_ue{{{b}}}) > 0",
            f"sum(increase(reaper_dimm_ce{{{b}}}[1h])) > 0",
            f"min(smartctl_device_smart_status{{{b}}}) == 0",
            f"max(reaper:sel_new{{{b}}}) > 0"])
        return f"({up} == 0) * 0 + 2 or ({warn}) * 0 + 1 or {up} * 0 or vector(-1)"
    if d.kind == "uplink":
        p = f'max(probe_success{{job="blackbox_internet_icmp",{S}}})'
        return f"({p} == 0) * 0 + 2 or {p} * 0 or vector(-1)"
    p = f'max(probe_success{{job="devices",{S},device="{d.name}",target_kind="mgmt"}})'
    ids = [f"L{i}" for i, l in enumerate(model.links)
           if l.expect == "up" and d.name in (l.a.device, l.b.device)]
    warn = (f' or (min(reaper:link_state{{{S},id=~"{"|".join(ids)}",live="1"}}) == 0) * 0 + 1'
            if ids else "")
    return f"({p} == 0) * 0 + 2{warn} or {p} * 0 or vector(-1)"


def _bmc_expr(d, site: str) -> str:
    S = f'site="{site}"'
    p = f'max(probe_success{{job="devices",{S},device="{d.name}",target_kind="bmc"}})'
    kcs = f'max(reaper_ipmi_kcs_ok{{{S},bang="{d.name}"}})'
    # unpinged (single-bang site: its own BMC is unreachable from itself): KCS answering = up
    return f"({p} == 0) * 0 + 2 or ({kcs} == 0) * 0 + 1 or {p} * 0 or {kcs} * 0 or vector(-1)"


def _end_expr(model, end, site: str):
    d = model.device(end.device)
    S = f'site="{site}"'
    if d is None or d.kind == "uplink":
        return None
    if d.kind == "bang":
        return f'max(node_network_up{{job="node",{S},bang="{d.name}",device="{_esc(end.port)}"}})'
    return (f'max(ifOperStatus{{job="snmp_switches",{S},switch="{d.name}",'
            f'ifName="{_esc(ifname(d.vendor, end.port))}"}}) == bool 1')


def _link_expr(model, link, site: str) -> str:
    parts = [(tag, e) for tag, e in (("a", _end_expr(model, link.a, site)), ("b", _end_expr(model, link.b, site)))
             if e]
    if not parts:
        return "vector(-1)"
    joined = " or ".join(f'label_replace({e}, "end", "{tag}", "", "")' for tag, e in parts)
    return f"min({joined}) or vector(-1)"


def _live(model, *names) -> bool:
    return all((model.device(n) is not None and model.device(n).state == "live") for n in names)


def _scope(c) -> str:
    if c.vip_gated:
        return "VIP holder"
    return {"primary": "primary", "standby": "standby"}.get(c.placement, "both bangs")


def rules_yaml(model, comps, sh, site: str, root: Path) -> str:
    S = f'site="{site}"'
    rec = []
    for d in model.devices:
        live = d.state == "live"
        rec.append({"record": "reaper:device_health",
                    "expr": _device_expr(d, model, site) if live else "vector(-1)",
                    "labels": {"site": site, "device": d.name, "kind": d.kind, "vendor": d.vendor,
                               "state": d.state, "live": str(int(live)), "mgmt_ip": d.mgmt_ip,
                               "bmc_ip": d.bmc_ip, "note": d.note}})
        if d.bmc_ip:
            rec.append({"record": "reaper:device_health",
                        "expr": _bmc_expr(d, site) if live else "vector(-1)",
                        "labels": {"site": site, "device": f"{d.name}-bmc", "kind": "bmc", "vendor": "",
                                   "state": d.state, "live": str(int(live)), "mgmt_ip": d.bmc_ip,
                                   "bmc_ip": "", "note": f"{d.name}'s BMC"}})
    for i, l in enumerate(model.links):
        live = _live(model, l.a.device, l.b.device)
        labels = {"site": site, "id": f"L{i}", "a": f"{l.a.device}:{l.a.port}",
                  "b": f"{l.b.device}:{l.b.port}", "speed": l.speed, "expect": l.expect,
                  "live": str(int(live)), "note": l.note}
        state = _link_expr(model, l, site)
        rec.append({"record": "reaper:link_state", "expr": state, "labels": labels})
        # what the board paints: 2 = down as expected, 3 = an end is not live; both grey
        if not live:
            display = "vector(3)"
        elif l.expect == "down":
            display = f"(({state}) == 0) * 0 + 2 or ({state})"
        else:
            display = state
        rec.append({"record": "reaper:link_display", "expr": display, "labels": labels})
    E = "reaper:expected"
    on = "on (site, bang, kind, name)"
    rec += [
        {"record": "reaper:expected",
         "expr": f"max by (site, bang, component, kind, name, vip_gated, optional) (reaper_expected{{{S}}})"},
        {"record": "reaper:vip_holder", "expr": f"max by (site, bang) (vrrp_state{{{S}}}) == 1"},
        {"record": "reaper:actual",       # one rule: promtool lints repeated record names
         "expr": (f'label_replace(max by (site, bang, name) (node_systemd_unit_state{{job="node",{S},'
                  f'state="active"}}), "kind", "unit", "", "")\n'
                  f'or label_replace(max by (site, bang, name) (reaper_container_running{{{S}}}), '
                  f'"kind", "container", "", "")\n'
                  f'or max by (site, bang, kind, name) (label_replace(label_replace(reaper_listening{{{S}}}, '
                  f'"kind", "$1", "proto", "(.*)"), "name", "$1", "port", "(.*)"))')},
        {"record": "reaper:expected_active",
         "expr": f'{E}{{{S},vip_gated="0"}} or ({E}{{{S},vip_gated="1"}} and on (site, bang) '
                 f'reaper:vip_holder{{{S}}})'},
        {"record": "reaper:expected_standby",
         "expr": f'{E}{{{S},vip_gated="1"}} unless on (site, bang) reaper:vip_holder{{{S}}}'},
        {"record": "reaper:item_state",
         "expr": (f"(reaper:expected_active{{{S}}} and {on} (reaper:actual{{{S}}} == 1)) * 0\n"
                  f"or (reaper:expected_active{{{S}}} and {on} (reaper:actual{{{S}}} == 0)) * 0 + 3\n"
                  f"or reaper:expected_standby{{{S}}} * 0 + 2\n"
                  f'or (reaper:expected_active{{{S},optional="1"}} unless {on} reaper:actual{{{S}}}) * 0 + 1\n'
                  f'or (reaper:expected_active{{{S},optional="0"}} unless {on} reaper:actual{{{S}}}) * 0 + 3')},
        {"record": "reaper:service_state",
         "expr": f"max by (site, bang, component) (reaper:item_state{{{S}}})"},
        {"record": "reaper:sel_new",
         "expr": (f"(max by (site, bang) (reaper_sel_entries{{{S}}}) - max by (site, bang) "
                  f"(reaper_sel_acked_entries{{{S}}})) >= 0 or max by (site, bang) (reaper_sel_entries{{{S}}})")},
    ] + [
        {"record": "reaper:component_info", "expr": "vector(1)",
         "labels": {"site": site, "component": c.name, "scope": _scope(c),
                    "ports": " · ".join(x for x in (
                        ("tcp " + ",".join(map(str, c.ports))) if c.ports else "",
                        ("udp " + ",".join(map(str, c.udp_ports))) if c.udp_ports else "") if x) or "-"}}
        for c in sorted(comps.values(), key=lambda c: c.name)
        if (c.units or c.containers or c.ports or c.udp_ports)
        and set(placed_on(c, sh, load_sites(Path(root))[site], Path(root))) - set(load_sites(Path(root))[site].dormant_hosts)
    ] + [
        {"record": "site:devices_unhealthy",
         "expr": f'count(reaper:device_health{{{S},live="1"}} >= 1) or vector(0)', "labels": {"site": site}},
        {"record": "site:links_down",
         "expr": f'count(reaper:link_state{{{S},expect="up",live="1"}} == 0) or vector(0)', "labels": {"site": site}},
        {"record": "site:services_down",
         "expr": f"count(reaper:service_state{{{S}}} == 3) or vector(0)", "labels": {"site": site}},
    ]

    def alert(name, expr, sev, summary, for_="2m"):
        return {"alert": name, "expr": expr, "for": for_, "labels": {"severity": sev, "site": site},
                "annotations": {"summary": summary}}
    alerts = [
        alert("DeviceDown", f'reaper:device_health{{{S},live="1"}} == 2', "critical",
              "{{ $labels.device }} down at {{ $labels.site }}"),
        alert("LinkDown", f'reaper:link_state{{{S},expect="up",live="1"}} == 0', "critical",
              "link {{ $labels.a }} <-> {{ $labels.b }} ({{ $labels.speed }}) down at {{ $labels.site }}"),
        alert("ServiceDown", f'max by (site, bang, component) (reaper:item_state{{{S},kind=~"unit|container"}}) == 3',
              "critical", "{{ $labels.component }} down on {{ $labels.bang }}"),
        alert("PortNotListening", f'reaper:item_state{{{S},kind=~"tcp|udp"}} == 3', "warning",
              "{{ $labels.component }}: nothing listens on {{ $labels.kind }} {{ $labels.name }} on {{ $labels.bang }}",
              "5m"),
        alert("DimmUncorrectable", f"sum by (site, bang, slot, serial) (reaper_dimm_ue{{{S}}}) > 0", "critical",
              "DIMM {{ $labels.slot }} ({{ $labels.serial }}) uncorrectable errors on {{ $labels.bang }}", "0m"),
        alert("DimmCorrectableRising", f"sum by (site, bang, slot, serial) (increase(reaper_dimm_ce{{{S}}}[1h])) > 0",
              "warning", "DIMM {{ $labels.slot }} ({{ $labels.serial }}) correctable errors rising on {{ $labels.bang }}",
              "0m"),
        alert("SmartFailed", f"smartctl_device_smart_status{{{S}}} == 0", "critical",
              "SMART failed on {{ $labels.bang }}:{{ $labels.device }}", "5m"),
        alert("IpmiSensorCritical", f"reaper_ipmi_sensor_ok{{{S}}} == 0", "critical",
              "IPMI sensor {{ $labels.sensor }} not ok on {{ $labels.bang }}", "5m"),
        alert("SelNewEntries", f"reaper:sel_new{{{S}}} > 0", "warning",
              "{{ $value }} new SEL entries on {{ $labels.bang }} (acknowledge: echo <count> | sudo tee "
              "/var/lib/reaper/sel-acked)", "0m"),
    ]
    doc = {"groups": [{"name": f"healthboard-{site}-records", "rules": rec},
                      {"name": f"healthboard-{site}-alerts", "rules": alerts}]}
    return "# generated by reaper scripts/health_board.py from devices.yml + components.yml: do not edit\n" + \
        yaml.safe_dump(doc, sort_keys=False, width=200)


# ---------------------------------------------------------------- one site
def write_site(root: Path, site: str, out: Path, domain: str = "") -> list:
    root, out = Path(root), Path(out)
    sites = load_sites(root)
    comps = load_components(root, sites)
    model = load_model(root, site)
    sh = site_hosts(sites[site], root, "reaper")
    paths = []

    def put(rel, text):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        paths.append(p)
    for b in sh.bangs:
        put(f"expected/{b.name}.prom", expected_prom(model, comps, sh, site, root, b.name))
    put(f"rules/healthboard-{site}.yml", rules_yaml(model, comps, sh, site, root))
    put("node-unit-include.txt", unit_include_regex(comps) + "\n")
    from reapercli import healthboard_dash as hd
    import json
    boards = {s: site_hosts(v, root, "reaper").primary for s, v in sites.items() if v.state == "active"}
    for name, dash in ((f"site-{site}", hd.site_dashboard(model, comps, sh, site, boards, domain)),
                       ("bang", hd.bang_dashboard()), ("switch", hd.switch_dashboard())):
        put(f"dashboards/{name}.json", json.dumps(dash, indent=1) + "\n")
    return paths
