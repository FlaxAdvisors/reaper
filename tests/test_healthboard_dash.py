"""Plan 2026-11-09-site-health-board Task 7: the site status board (layout B) and the
bang / switch drill-downs, generated as Grafana 11 JSON."""
import json
import re
from pathlib import Path

from reapercli import healthboard_dash as hd
from reapercli.lib.components import load_components
from reapercli.lib.inventory import site_hosts
from reapercli.lib.sitemodel import load_model
from reapercli.lib.sites import load_sites

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)


def _site(site="eindhoven"):
    return hd.site_dashboard(load_model(ROOT, site), COMPS, site_hosts(SITES[site], ROOT, "orbit"), site)


def _panels(d):
    out = []
    for p in d["panels"]:
        out += [p] + list(p.get("panels") or [])
    return out


def _exprs(p):
    return [t.get("expr", "") for t in p.get("targets") or []]


def test_site_board_identity_and_datasource():
    d = _site()
    assert d["uid"] == "site-eindhoven" and d["title"] == "Site health · eindhoven"
    for p in _panels(d):
        if p.get("targets"):
            assert p["datasource"] == {"type": "prometheus", "uid": "prometheus"}, p["title"]


def test_every_device_has_a_tile_with_the_four_states():
    m = load_model(ROOT, "eindhoven")
    tiles = {}
    for p in _panels(d := _site()):
        for e in _exprs(p):
            hit = re.search(r'reaper:device_health\{site="eindhoven",device="([^"]+)"\}', e)
            if hit and p["type"] == "stat":
                tiles[hit.group(1)] = p
    want = {x.name for x in m.devices} | {f"{x.name}-bmc" for x in m.devices if x.bmc_ip}
    assert want <= set(tiles)
    maps = json.dumps(tiles["bang-gouda"]["fieldConfig"]["defaults"]["mappings"])
    for word in ("UP", "WARN", "DOWN", "no data"):
        assert word in maps
    assert "/d/bang?" in json.dumps(tiles["bang-gouda"]) and "/d/switch?" in json.dumps(tiles["rabbit-gouda"])


def test_links_and_services_tables():
    ps = {p["title"]: p for p in _panels(_site())}
    links, svc = ps["Links"], ps["Services, containers and ports"]
    assert links["type"] == "table" and 'reaper:link_display{site="eindhoven"}' in _exprs(links)[0]
    assert svc["type"] == "table" and 'reaper:service_state{site="eindhoven"}' in _exprs(svc)[0]
    m = json.dumps(svc["fieldConfig"])
    for word in ("ok", "n/a", "standby", "down"):
        assert f'"{word}"' in m


def test_resource_gauges_per_bang_with_thresholds():
    gauges = [p for p in _panels(_site()) if p["type"] == "gauge"]
    titles = {p["title"] for p in gauges}
    for bang in ("bang-gouda", "bang-edam"):
        for what in ("disk", "memory used", "eth0", "eth1", "load / cores"):
            assert any(bang in t and what in t for t in titles), (bang, what)
    pct = [p for p in gauges if "pressure" not in p["title"]]
    for p in pct:
        steps = [s.get("value") for s in p["fieldConfig"]["defaults"]["thresholds"]["steps"]]
        assert steps[1:] == [70, 90], p["title"]
    assert any("pressure" in p["title"] for p in _panels(_site()))


def test_no_panel_names_a_device_outside_the_model():
    m = load_model(ROOT, "eindhoven")
    known = {x.name for x in m.devices} | {f"{x.name}-bmc" for x in m.devices if x.bmc_ip}
    for p in _panels(_site()):
        for e in _exprs(p):
            for dev in re.findall(r'device="([^"]+)"', e):
                assert dev in known or dev in ("eth0", "eth1"), (p["title"], dev)


def test_drilldowns_are_parameterised():
    b, s = hd.bang_dashboard(), hd.switch_dashboard()
    assert b["uid"] == "bang" and s["uid"] == "switch"
    assert {v["name"] for v in b["templating"]["list"]} == {"bang"}
    assert {v["name"] for v in s["templating"]["list"]} == {"switch"}
    assert any("reaper_dimm_ue" in e for p in _panels(b) for e in _exprs(p))
    assert any("ifOperStatus" in e for p in _panels(s) for e in _exprs(p))


def test_write_site_writes_dashboards(tmp_path):
    from reapercli.healthboard import write_site
    names = {p.relative_to(tmp_path).as_posix() for p in write_site(ROOT, "eindhoven", tmp_path, "bangs.example.net")}
    assert {"dashboards/site-eindhoven.json", "dashboards/bang.json", "dashboards/switch.json"} <= names
    for n in names:
        if n.endswith(".json"):
            json.loads((tmp_path / n).read_text())


def test_load_gauge_divides_by_a_label_compatible_core_count():
    """node_load5{site,bang,...} / count(...) matches nothing (count drops the labels): the gauge
    read "No data" on every bang (headless check 2026-11-09)."""
    d = _site("eindhoven")
    load = [p for p in d["panels"] if p.get("title", "").endswith("load / cores")]
    assert load
    for p in load:
        expr = p["targets"][0]["expr"]
        assert "/ count(" not in expr
        assert "scalar(count(node_cpu_seconds_total" in expr


def _summary(d):
    """The federated summary row's panels (between its row header and the next row)."""
    out, inside = [], False
    for p in d["panels"]:
        if p["type"] == "row":
            inside = p["title"].startswith("Sites")
            continue
        if inside:
            out.append(p)
    return out


def test_summary_has_one_tile_per_site_named_and_linked_to_its_board(tmp_path):
    """Operator 2026-11-09: label the summary tiles by site and link each to that site's board."""
    from reapercli.healthboard import write_site
    write_site(ROOT, "lavergne", tmp_path, "bangs.example.net")
    d = json.loads((tmp_path / "dashboards" / "site-lavergne.json").read_text())
    tiles = {p["title"]: p for p in _summary(d)}
    assert set(tiles) == {"eindhoven", "lavergne (this site)", "braintree"}
    for site, primary in (("eindhoven", "bang-gouda"), ("braintree", "bang-fiesta")):
        assert tiles[site]["links"][0]["url"] == f"https://health.{primary}.bangs.example.net/d/site-{site}"
    assert tiles["lavergne (this site)"]["links"][0]["url"] == "/d/site-lavergne"
    for site, p in (("eindhoven", tiles["eindhoven"]), ("braintree", tiles["braintree"])):
        exprs = _exprs(p)
        assert exprs == [f'site:devices_unhealthy{{site="{site}"}}', f'site:links_down{{site="{site}"}}',
                         f'site:services_down{{site="{site}"}}']
        assert [t["legendFormat"] for t in p["targets"]] == ["devices", "links", "services"]
        assert len({t["refId"] for t in p["targets"]}) == 3
        assert p["options"]["textMode"] == "value_and_name"
