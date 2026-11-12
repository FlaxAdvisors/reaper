"""Plan 2026-11-09-site-health-board Task 8: the roles generate the board on the controller
and ship it; the old dashboards and superseded rules go; one local datasource; the
prometheus config pings every live device, labels SNMP/internet by site, and federates
lavergne through bang-rhythm."""
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.plugins.loader import init_plugin_loader  # noqa: E402
from ansible.template import Templar  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "roles/apply_health_compose"
HOOKS = ROOT / "roles/apply_health_metrics_hooks"


def _tasks(role):
    return yaml.safe_load((role / "tasks/main.yml").read_text())


def _gen_task(role):
    return [t for t in _tasks(role) if "health_board.py" in str(t.get("ansible.builtin.command", ""))]


def test_both_roles_pass_the_lab_domain_to_the_generator():
    """site_dashboard links other sites' boards by <bang>.<bang_domain> and refuses without it,
    so every generator call passes --domain (apply_health_metrics_hooks lacked it once)."""
    for role in (COMPOSE, HOOKS):
        [t] = _gen_task(role)
        assert "--domain {{ bang_domain }}" in t["ansible.builtin.command"], role.name


def test_both_roles_generate_on_the_controller():
    for role in (COMPOSE, HOOKS):
        [t] = _gen_task(role)
        assert t["delegate_to"] == "localhost" and t["run_once"] is True and t["become"] is False


def test_compose_stages_generated_rules_and_dashboards():
    text = (COMPOSE / "tasks/main.yml").read_text()
    assert "healthboard-{{ site_name }}.yml" in text
    for name in ("site-{{ site_name }}.json", "bang.json", "switch.json"):
        assert name in text


def test_one_local_datasource_and_the_old_three_deleted():
    t = (COMPOSE / "templates/grafana/datasources.yml.j2").read_text()
    doc = yaml.safe_load(t)
    assert [d["uid"] for d in doc["datasources"]] == ["prometheus"]
    assert doc["datasources"][0]["url"] == "http://localhost:9090"
    assert sorted(d["name"] for d in doc["deleteDatasources"]) == ["braintree", "eindhoven", "lavergne"]


def _render(site, me, hosts):
    init_plugin_loader()
    hv = {h: {"bang_tailscale_ip": ts, "bang_mgmt_ip": mg, "site_name": site, "ansible_host": mg}
          for h, (ts, mg) in hosts.items()}
    v = {"inventory_hostname": me, "role_path": str(COMPOSE), "site_name": site,
         "groups": {"jumphosts": [h for h in hosts if h.startswith("bang-")], "arista": [h for h in hosts if h.startswith("rabbit")],
                    "cumulus": [], "cisco": [h for h in hosts if h.startswith("turtle")]},
         "hostvars": hv, "bang_domain": "bangs.example.net", **hv[me], **yaml.safe_load((COMPOSE / "defaults/main.yml").read_text())}
    t = Templar(loader=DataLoader(), variables=v)
    return yaml.safe_load(t.template((COMPOSE / "templates/prometheus/prometheus.yml.j2").read_text()))


EHV = {"bang-gouda": ("100.64.0.201", "192.168.88.21"), "bang-edam": ("100.64.0.205", "192.168.88.22"),
       "turtle-gouda": ("", "192.168.88.33"), "rabbit-gouda": ("", "192.168.88.31"),
       "rabbit-edam": ("", "192.168.88.32")}


def _job(doc, name):
    return next(j for j in doc["scrape_configs"] if j["job_name"] == name)


def test_devices_job_pings_every_live_device_and_bmc():
    doc = _render("eindhoven", "bang-gouda", EHV)
    devs = {(sc["labels"]["device"], sc["labels"]["target_kind"]): sc["targets"][0]
            for sc in _job(doc, "devices")["static_configs"]}
    assert devs[("bang-gouda", "bmc")] == "192.168.88.11" and devs[("turtle-gouda", "mgmt")] == "192.168.88.33"
    assert all(sc["labels"]["site"] == "eindhoven" for sc in _job(doc, "devices")["static_configs"])
    assert not any(j["job_name"] == "blackbox_own_site_switches" for j in doc["scrape_configs"])


def test_snmp_and_internet_jobs_carry_the_site_label():
    doc = _render("eindhoven", "bang-gouda", EHV)
    assert all(sc["labels"]["site"] == "eindhoven" for sc in _job(doc, "snmp_switches")["static_configs"])
    assert _job(doc, "blackbox_internet_icmp")["static_configs"][0]["labels"]["site"] == "eindhoven"


def test_federation_reaches_lavergne_through_rhythm_only():
    doc = _render("eindhoven", "bang-gouda", EHV)
    feds = [j for j in doc["scrape_configs"] if j["job_name"].startswith("federate")]
    targets = {t: j for j in feds for sc in j["static_configs"] for t in sc["targets"]}
    assert set(targets) == {"bang-rhythm.bangs.example.net:9090", "bang-fiesta.bangs.example.net:9090"}
    assert targets["bang-rhythm.bangs.example.net:9090"]["params"]["match[]"] == ['{__name__=~"site:.+",site="lavergne"}']


def test_snmp_modules_define_interface_metrics_with_ifname():
    snmp = (COMPOSE / "templates/snmp/snmp.yml.j2").read_text()
    for name in ("ifOperStatus", "ifHighSpeed", "ifHCInOctets", "ifHCOutOctets", "ifInErrors", "ifInDiscards"):
        assert f"name: {name}" in snmp
    assert "labelname: ifName" in snmp and "1.3.6.1.2.1.31.1.1.1.1" in snmp


def test_hooks_ship_each_bangs_expected_file():
    text = (HOOKS / "tasks/main.yml").read_text()
    assert "expected/{{ inventory_hostname }}.prom" in text and "expected.prom" in text


def test_node_exporter_unit_list_comes_from_the_generator():
    t = (ROOT / "roles/apply_node_exporter/templates/prometheus-node_exporter.service.d/override.conf.j2").read_text()
    assert "apply_node_exporter_unit_include" in t
    d = (ROOT / "roles/apply_node_exporter/defaults/main.yml").read_text()
    assert "health_board.py --unit-include" in d



# review 9: an offline switch is not SNMP-scraped (PrometheusTargetDown forever)
def test_snmp_targets_are_the_live_switches_of_devices_yml():
    LVQ = {"bang-rhythm": ("100.1.1.1", "192.168.88.21"), "bang-blues": ("", "192.168.88.22"),
           "turtle-rhythm": ("", "192.168.88.33"), "rabbit-rhythm": ("", "192.168.88.31"),
           "rabbit-c2g": ("", "192.168.88.35")}
    doc = _render("lavergne", "bang-rhythm", LVQ)
    sw = {sc["labels"]["switch"]: sc for sc in _job(doc, "snmp_switches")["static_configs"]}
    assert "rabbit-c2g" not in sw and sw["turtle-rhythm"]["labels"]["module"] == "cumulus_linux"
    assert sw["rabbit-howlin"]["targets"] == ["192.168.88.34"]


# deploy finding 2026-11-09: a bang cannot reach its own BMC (NC-SI on its own NIC)
def test_a_bangs_own_bmc_is_pinged_through_its_peer():
    doc = _render("eindhoven", "bang-gouda", EHV)
    via = {(sc["labels"]["device"], sc["labels"]["target_kind"]): sc["labels"]["probe_via"]
           for sc in _job(doc, "devices")["static_configs"]}
    assert via[("bang-gouda", "bmc")] == "100.64.0.205"          # edam's blackbox
    assert via[("bang-edam", "bmc")] == "100.64.0.201"          # its own (gouda's) blackbox
    relabel = str(_job(doc, "devices")["relabel_configs"])
    assert "probe_via" in relabel and "labeldrop" in relabel


def test_no_peer_no_self_bmc_ping():
    BRT = {"bang-fiesta": ("100.64.0.204", "192.168.88.21"), "bang-siesta": ("100.64.0.203", "192.168.88.22"),
           "turtle-lorax": ("", "192.168.88.33"), "rabbit-lorax": ("", "192.168.88.31")}
    doc = _render("braintree", "bang-fiesta", BRT)
    kinds = {(sc["labels"]["device"], sc["labels"]["target_kind"]) for sc in _job(doc, "devices")["static_configs"]}
    assert ("bang-fiesta", "mgmt") in kinds and ("bang-fiesta", "bmc") not in kinds


def test_a_changed_snmp_config_reloads_the_exporter():
    [t] = [t for t in _tasks(COMPOSE) if t["name"] == "Stage snmp.yml"]
    assert t["notify"] == "Reload snmp_exporter"
    h = yaml.safe_load((COMPOSE / "handlers/main.yml").read_text())
    [r] = [x for x in h if x["name"] == "Reload snmp_exporter"]
    assert "prod-health-snmp_exporter-1" in str(r) and "HUP" in str(r)


def test_a_changed_datasource_restarts_grafana():
    # Grafana applies datasource provisioning (incl. deleteDatasources) only at startup
    [t] = [t for t in _tasks(COMPOSE) if t["name"] == "Stage grafana datasource provisioning"]
    assert t["notify"] == "Restart grafana"
    h = yaml.safe_load((COMPOSE / "handlers/main.yml").read_text())
    [r] = [x for x in h if x["name"] == "Restart grafana"]
    assert "docker restart prod-health-grafana-1" in str(r)
