"""Fix pass from the whole-branch review of plan 2026-11-09-site-health-board."""
import json
import re
from pathlib import Path

import yaml

from reapercli import healthboard as hb
from reapercli import healthboard_dash as hd
from reapercli.lib.components import load_components
from reapercli.lib.inventory import site_hosts
from reapercli.lib.sitemodel import load_model
from reapercli.lib.sites import load_sites

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)


def _ctx(site):
    return load_model(ROOT, site), COMPS, site_hosts(SITES[site], ROOT, "orbit"), site, ROOT


def _recs(site):
    d = yaml.safe_load(hb.rules_yaml(*_ctx(site)))
    return [r for g in d["groups"] for r in g["rules"]]


# 1 (critical): a static textfile must not trip TextfileCollectorStale forever
def test_expected_prom_is_exempt_from_the_stale_textfile_rule():
    meta = (ROOT / "roles/apply_health_compose/files/prometheus/rules/meta.yml").read_text()
    expr = re.search(r"alert: TextfileCollectorStale\s+expr: (.*)", meta).group(1)
    assert 'file!~".*expected.prom"' in expr


# 2 (critical): a timer-driven oneshot is judged by its timer, not by the idle service
def test_a_unit_with_a_timer_is_expected_as_the_timer():
    t = hb.expected_prom(*_ctx("eindhoven"), "bang-gouda")
    assert 'name="bang-wan-route-guard.timer"' in t
    assert 'name="bang-wan-route-guard.service"' not in t


# 5: the links table never paints an expected state red
def test_links_display_folds_expect_and_live():
    recs = {r["labels"]["id"]: r for r in _recs("eindhoven") if r.get("record") == "reaper:link_display"}
    assert recs["L6"]["labels"]["expect"] == "down" and "* 0 + 2" in recs["L6"]["expr"]
    bt = {r["labels"]["b"]: r for r in _recs("braintree") if r.get("record") == "reaper:link_display"}
    assert bt["rabbit-lorax:Et32/4"]["expr"] == "vector(3)"
    d = hd.site_dashboard(load_model(ROOT, "eindhoven"), COMPS, site_hosts(SITES["eindhoven"], ROOT, "o"), "eindhoven")
    [links] = [p for p in d["panels"] if p.get("title") == "Links"]
    assert "reaper:link_display" in links["targets"][0]["expr"]
    m = json.dumps(links["fieldConfig"])
    assert "down (expected)" in m and "not live" in m


# 8: every task that templates the community is no_log
def test_switch_snmp_tasks_that_template_the_community_are_no_log():
    for vendor in ("arista", "cisco", "cumulus"):
        for t in yaml.safe_load((ROOT / f"roles/apply_switch_snmp/tasks/{vendor}.yml").read_text()):
            if "apply_switch_snmp_community" in str(t):
                assert t.get("no_log") is True, (vendor, t["name"])


# 17: "UNCHANGED" must not count as changed
def test_cumulus_snmp_edit_changed_only_on_changed():
    [t] = [t for t in yaml.safe_load((ROOT / "roles/apply_switch_snmp/tasks/cumulus.yml").read_text())
           if t.get("register") == "apply_switch_snmp_edit"]
    assert "stdout_lines" in t["changed_when"]


# 11: federation pulls only each remote site's own summary (no echo of our own series)
def test_federation_is_one_job_per_remote_site_matching_only_its_summary():
    text = (ROOT / "roles/apply_health_compose/templates/prometheus/prometheus.yml.j2").read_text()
    fed = text[text.index("job_name: federate"):]
    assert """- '{__name__=~"site:.+",site="{{ s }}"}'""" in fed   # the match[] itself, not the comment
    assert "node_.+" not in fed and "probe_success" not in fed


# 12: one dead switch is one page, not one per link
def test_device_down_inhibits_its_link_downs():
    am = (ROOT / "roles/apply_health_compose/templates/alertmanager/alertmanager.yml.j2").read_text()
    assert re.search(r'source_matchers: \[alertname="DeviceDown"\]\s+target_matchers:\s+- alertname="LinkDown"', am)


# 15: NIC gauges show the busier direction, not receive only
def test_nic_gauge_takes_the_max_of_rx_and_tx():
    d = hd.site_dashboard(load_model(ROOT, "eindhoven"), COMPS, site_hosts(SITES["eindhoven"], ROOT, "o"), "eindhoven")
    [g] = [p for p in d["panels"] if p.get("title") == "bang-gouda · eth1"]
    e = g["targets"][0]["expr"]
    assert 'label_replace' in e and '"dir", "rx"' in e and '"dir", "tx"' in e


def test_bmc_health_falls_back_to_kcs_when_unpinged():
    [r] = [r for r in _recs("braintree") if r.get("record") == "reaper:device_health"
           and r["labels"]["device"] == "bang-fiesta-bmc"]
    assert r["expr"].rstrip().endswith('max(reaper_ipmi_kcs_ok{site="braintree",bang="bang-fiesta"}) * 0 or vector(-1)')
