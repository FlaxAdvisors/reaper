"""apply_health_compose prometheus.yml.j2 (FOLLOWUP-2026-11-08-health-noise §3): a host
files/reaper/sites.yml marks dormant (bang-siesta) is no scrape, federate, peer-probe or
alertmanager target, so a dead standby is not permanent alert noise. Renders the real
template through Ansible's Templar (select('match'), lookup, from_yaml as in a play)."""
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.plugins.loader import init_plugin_loader  # noqa: E402
from ansible.template import Templar  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/apply_health_compose"
HOSTS = {"bang-fiesta": ("100.64.0.202", "192.168.88.21"),
         "bang-siesta": ("100.64.0.203", "192.168.88.22")}


def render(name="prometheus/prometheus.yml.j2", me="bang-fiesta", **over):
    init_plugin_loader()
    hv = {h: {"bang_tailscale_ip": ts, "bang_mgmt_ip": mg, "site_name": "braintree"}
          for h, (ts, mg) in HOSTS.items()}
    v = {"inventory_hostname": me, "role_path": str(ROLE), "site_name": "braintree",
         "groups": {"jumphosts": list(HOSTS), "arista": [], "cumulus": [], "cisco": []},
         "hostvars": hv, "bang_domain": "bangs.example.net", **hv[me], **yaml.safe_load((ROLE / "defaults/main.yml").read_text()), **over}
    t = Templar(loader=DataLoader(), variables=v)
    return t.template((ROLE / f"templates/{name}").read_text())


def test_the_dormant_standby_is_nowhere_in_the_config():
    text = render()
    assert "100.64.0.203" not in text and "192.168.88.22" not in text and "bang-siesta" not in text


def test_the_live_bang_is_still_scraped_and_federation_still_reaches_other_sites():
    doc = yaml.safe_load(render())
    jobs = {j["job_name"]: j for j in doc["scrape_configs"]}
    node = [t for sc in jobs["node"]["static_configs"] for t in sc["targets"]]
    assert node == ["100.64.0.202:9100"]
    fed = [t for sc in jobs["federate-eindhoven"]["static_configs"] for t in sc["targets"]]
    assert fed == ["bang-gouda.bangs.example.net:9090"]


def _peer_flags(text):
    import re
    return re.findall(r'(--cluster\.peer=[^"\s]*)', text)


def test_the_dormant_standby_is_no_alertmanager_gossip_peer():
    # braintree's only live bang has NO peer: no flag at all. An empty
    # `--cluster.peer=` stops alertmanager ("missing port in address"):
    # fiesta restart-looped 223 times after 5bbaf53 (2026-11-09).
    assert _peer_flags(render("docker-compose-prod.yml.j2")) == []
    # presence proof: with nothing dormant the same render names the standby, one flag per peer
    assert _peer_flags(render("docker-compose-prod.yml.j2", apply_health_compose_dormant=[])) == [
        "--cluster.peer=100.64.0.203:9094"]
    assert "100.64.0.203" in render(apply_health_compose_dormant=[])


def test_prometheus_retention_follows_the_role_vars():
    default = render("docker-compose-prod.yml.j2")
    assert "--storage.tsdb.retention.size=40GB" in default and "--storage.tsdb.retention.time=90d" in default
    small = render("docker-compose-prod.yml.j2", apply_health_compose_retention_size="20GB")
    assert "--storage.tsdb.retention.size=20GB" in small and "retention.size=40GB" not in small


def test_braintree_caps_prometheus_below_its_free_root_disk():
    site = yaml.safe_load((ROOT / "inventories/braintree/group_vars/all/vars.yml").read_text())
    assert site["apply_health_compose_retention_size"] == "20GB"
