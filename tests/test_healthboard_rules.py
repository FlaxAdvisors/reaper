"""Plan 2026-11-09-site-health-board Task 6: expectations and rules generated from the
site model + components.yml. Codes: item/service 0 ok, 1 n/a, 2 standby, 3 down;
device -1 no data / not live, 0 ok, 1 warn, 2 down; link -1 no data, 0 down, 1 up."""
import re
from pathlib import Path

import yaml

from reapercli import healthboard as hb
from reapercli.lib.components import load_components, placed_on
from reapercli.lib.inventory import site_hosts
from reapercli.lib.sitemodel import load_model
from reapercli.lib.sites import load_sites

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)


def _ctx(site):
    return load_model(ROOT, site), COMPS, site_hosts(SITES[site], ROOT, "orbit"), site, ROOT


def test_expected_for_the_primary_has_vip_gated_kea_and_keepalived():
    t = hb.expected_prom(*_ctx("eindhoven"), "bang-gouda")
    assert 'reaper_expected{component="kea",kind="unit",name="kea-dhcp4.service",vip_gated="1",optional="0"} 1' in t
    assert 'reaper_expected{component="kea",kind="udp",name="67",vip_gated="1",optional="0"} 1' in t
    assert 'reaper_expected{component="keepalived",kind="unit",name="keepalived.service",vip_gated="0",optional="0"} 1' in t
    assert 'kind="container",name="kea-dhcp4"' in t


def test_single_placed_components_appear_only_where_placed():
    sh = site_hosts(SITES["eindhoven"], ROOT, "orbit")
    for bang in ("bang-gouda", "bang-edam"):
        t = hb.expected_prom(*_ctx("eindhoven"), bang)
        for c in COMPS.values():
            here = bang in placed_on(c, sh, SITES["eindhoven"], ROOT)
            assert (f'component="{c.name}"' in t) == (here and bool(c.units or c.containers or c.ports
                                                                       or c.udp_ports)), (bang, c.name)


def test_a_dormant_bang_expects_nothing():
    assert "reaper_expected{" not in hb.expected_prom(*_ctx("braintree"), "bang-siesta")


def _rules(site="eindhoven"):
    return yaml.safe_load(hb.rules_yaml(*_ctx(site)))


def _exprs(site="eindhoven"):
    return [r["expr"] for g in _rules(site)["groups"] for r in g["rules"]]


def test_every_raw_selector_is_scoped_to_its_site():
    for e in _exprs():
        for sel in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\{([^}]*)\}", e):
            name, body = sel
            if name.startswith("reaper:") or name in ("vector", "label_replace"):
                continue
            assert 'site="eindhoven"' in body, f"{name}{{{body}}}"


def test_arista_short_port_names_become_snmp_ifnames():
    text = hb.rules_yaml(*_ctx("eindhoven"))
    assert 'ifName="Ethernet29/3"' in text and 'ifName="Management1"' in text
    assert 'ifName="Te1/1/1"' in text                       # cisco ifName is the short form


def test_every_live_device_and_link_has_a_rule():
    m = load_model(ROOT, "eindhoven")
    recs = [r for g in _rules()["groups"] for r in g["rules"] if "record" in r]
    dev = {r["labels"]["device"] for r in recs if r["record"] == "reaper:device_health"}
    assert {d.name for d in m.devices} <= dev
    assert {f"{d.name}-bmc" for d in m.devices if d.bmc_ip} <= dev
    links = [r for r in recs if r["record"] == "reaper:link_state"]
    assert len(links) == len(m.links)


def test_no_alert_for_an_expected_down_or_not_live_link():
    alerts = {r["alert"]: r for g in _rules()["groups"] for r in g["rules"] if "alert" in r}
    assert 'expect="up"' in alerts["LinkDown"]["expr"] and 'live="1"' in alerts["LinkDown"]["expr"]
    assert set(alerts) >= {"DeviceDown", "LinkDown", "ServiceDown", "PortNotListening", "DimmUncorrectable",
                           "DimmCorrectableRising", "SmartFailed", "IpmiSensorCritical", "SelNewEntries"}
    assert {alerts[a]["labels"]["severity"] for a in ("DeviceDown", "LinkDown", "ServiceDown",
                                                      "DimmUncorrectable", "SmartFailed")} == {"critical"}


def test_unit_include_regex_covers_every_component_unit():
    rx = re.compile(hb.unit_include_regex(COMPS))
    for c in COMPS.values():
        for u in c.units:
            assert rx.fullmatch(f"{u}.service"), u
    assert rx.fullmatch("health-hook@bang_hw.service") and not rx.fullmatch("kdump.service")


def test_write_site_writes_the_artefacts(tmp_path):
    paths = hb.write_site(ROOT, "eindhoven", tmp_path, "bangs.example.net")
    names = {p.relative_to(tmp_path).as_posix() for p in paths}
    assert {"expected/bang-gouda.prom", "expected/bang-edam.prom", "rules/healthboard-eindhoven.yml",
            "node-unit-include.txt"} <= names
