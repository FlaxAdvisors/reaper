"""Lab-domain UI names (operator 2026-10-27): triage./post./console./view. under
both <bang>.<lab_domain> and bang.<lab_domain>, and the post fleet viewer (view.)
deployable on a per-site host list instead of hard-wired to bang-gouda."""
import os
import re

import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def read(*p):
    with open(os.path.join(_REAPER, *p)) as f:
        return f.read()


DNSMASQ = read("roles", "apply_bang_services", "templates", "dnsmasq.conf.j2")
LAB_UI = read("roles", "apply_bang_services", "templates", "nginx-lab-ui.conf.j2")


def test_every_ui_name_has_both_cnames():
    for svc in ("triage", "post", "view", "console"):
        assert "cname=%s.{{ bang_name }}.{{ bang_lab_domain }}," % svc in DNSMASQ, svc
        assert "cname=%s.bang.{{ bang_lab_domain }}," % svc in DNSMASQ, svc


def test_console_lab_vhost_proxies_flax_control():
    m = re.search(r"server_name console\.\{\{ inventory_hostname \}\}\.\{\{ bang_lab_domain \}\}\s*\n\s*"
                  r"console\.bang\.\{\{ bang_lab_domain \}\};(.*?)\n\}", LAB_UI, re.S)
    assert m, "console vhost missing"
    assert "proxy_pass         http://127.0.0.1:8000;" in m.group(1)


def test_fleet_viewer_gated_by_host_list():
    pb = read("playbooks", "apply_bang_services.yml")
    assert "when: inventory_hostname == 'bang-gouda'" not in pb
    assert "when: inventory_hostname in apply_post_fleet_viewer_hosts" in pb
    placement = yaml.safe_load(read("playbooks", "group_vars", "all", "placement.yml"))
    assert placement["apply_post_fleet_viewer_hosts"] == "{{ site_bangs }}"   # both bangs, every site
