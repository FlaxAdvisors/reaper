"""Site health board at a URI (operator 2026-11-09): https://health.<bang>.<bang_domain>/
and the on-site https://health.<bang>.<lab_domain>/ + https://health.bang.<lab_domain>/, all
fronting the bang's Grafana (:3000), on the fleet-CA leaf apply_flax_tls signs from the vault."""
import os
import re

import jinja2
import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def read(*p):
    with open(os.path.join(_REAPER, *p)) as f:
        return f.read()


def _server(conf, first_name):
    """The server { } block whose server_name starts with first_name."""
    m = re.search(r"server \{\n((?:(?!\nserver \{).)*?server_name " + re.escape(first_name) + r".*?)\n\}",
                  conf, re.S)
    assert m, f"no server block for {first_name}"
    return m.group(1)


def test_bang_domain_and_lab_names_terminate_tls_and_proxy_grafana():
    blk = _server(read("roles", "apply_flax_stack", "templates", "nginx.conf.j2"),
                  "health.{{ inventory_hostname }}.{{ bang_domain }}")
    assert "listen 443 ssl;" in blk
    for name in ("health.{{ inventory_hostname }}.{{ bang_lab_domain }}", "health.bang.{{ bang_lab_domain }}"):
        assert name in blk
    assert "ssl_certificate     /etc/nginx/tls/server.crt;" in blk
    assert "proxy_pass         http://127.0.0.1:3000;" in blk
    assert "Upgrade" in blk                       # grafana live (websocket) channel


def test_lab_http_names_redirect_to_https():
    blk = _server(read("roles", "apply_bang_services", "templates", "nginx-lab-ui.conf.j2"),
                  "health.{{ inventory_hostname }}.{{ bang_lab_domain }}")
    assert "listen 80;" in blk
    assert "return 301 https://$host$request_uri;" in blk


def test_dnsmasq_has_both_health_cnames():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(
        os.path.join(_REAPER, "roles", "apply_bang_services", "templates")))
    src = env.loader.get_source(env, "dnsmasq.conf.j2")[0]
    assert "cname=health.{{ bang_name }}.{{ bang_lab_domain }},{{ bang_name }}.{{ bang_lab_domain }}" in src
    assert "cname=health.bang.{{ bang_lab_domain }},bang.{{ bang_lab_domain }}" in src


def _tls_defaults(**hostvars):
    """apply_flax_tls_sans as ansible resolves it (base list + the lab expression)."""
    from jinja2.nativetypes import NativeEnvironment
    raw = yaml.safe_load(read("roles", "apply_flax_tls", "defaults", "main.yml"))
    env = NativeEnvironment()
    env.filters["default"] = lambda v, d="": d if isinstance(v, jinja2.Undefined) else v
    ctx = dict(inventory_hostname="bang-gouda", bang_domain="bangs.example.net", **hostvars)
    ctx["apply_flax_tls_domain"] = env.from_string(raw["apply_flax_tls_domain"]).render(**ctx)
    ctx["apply_flax_tls_sans_base"] = [env.from_string(s).render(**ctx) for s in raw["apply_flax_tls_sans_base"]]
    ctx["apply_flax_tls_sans_lab"] = env.from_string(raw["apply_flax_tls_sans_lab"]).render(**ctx)
    return env.from_string(raw["apply_flax_tls_sans"]).render(**ctx), raw


def test_leaf_covers_the_lab_wildcards():
    sans, _ = _tls_defaults(bang_lab_domain="ehv.lab")
    assert "DNS:*.bang-gouda.ehv.lab" in sans
    assert "DNS:*.bang.ehv.lab" in sans
    assert "DNS:*.bang-gouda.bangs.example.net" in sans


def test_leaf_without_a_lab_domain_has_no_lab_sans():
    sans, _ = _tls_defaults()
    assert not [s for s in sans if ".lab" in s or s.endswith(".") or "None" in s or not s.split(":", 1)[1]]


def _needs_leaf(existing_stdout, rc=0, **hostvars):
    tasks = yaml.safe_load(read("roles", "apply_flax_tls", "tasks", "main.yml"))
    expr = next(t for t in tasks if t.get("name") == "Decide whether the leaf needs (re)generation"
                )["ansible.builtin.set_fact"]["apply_flax_tls_needs_leaf"]
    sans, _ = _tls_defaults(**hostvars)
    env = jinja2.Environment()
    out = env.from_string(expr).render(
        apply_flax_tls_existing_san={"rc": rc, "stdout": existing_stdout},
        apply_flax_tls_sans=sans, apply_flax_tls_domain="bangs.example.net", inventory_hostname="bang-gouda")
    return out.strip() == "True"


OLD_LEAF = ("X509v3 Subject Alternative Name: \n    DNS:*.bang-gouda.bangs.example.net, "
            "DNS:bang-gouda.bangs.example.net, DNS:bang-gouda, DNS:localhost, IP Address:127.0.0.1")
NEW_LEAF = OLD_LEAF + ", DNS:*.bang-gouda.ehv.lab, DNS:*.bang.ehv.lab"


def test_a_leaf_missing_a_new_san_is_reissued():
    assert _needs_leaf(OLD_LEAF, bang_lab_domain="ehv.lab")


def test_a_leaf_with_every_san_is_kept():
    assert not _needs_leaf(NEW_LEAF, bang_lab_domain="ehv.lab")


def test_no_leaf_is_issued():
    assert _needs_leaf("", rc=1, bang_lab_domain="ehv.lab")


def test_grafana_root_url_is_the_health_name():
    src = read("roles", "apply_health_compose", "templates", "docker-compose-prod.yml.j2")
    assert "GF_SERVER_ROOT_URL=https://health.{{ inventory_hostname }}.{{ bang_domain }}/" in src


def test_dnsmasq_tag_ships_what_the_conf_needs():
    """`--tags dnsmasq` must also create the log dir the conf's log-facility names (bang-fiesta
    had none on 2026-11-09: the conf alone would have stopped dnsmasq)."""
    tasks = yaml.safe_load(read("roles", "apply_bang_services", "tasks", "main.yml"))
    tagged = [t for t in tasks if "dnsmasq" in (t.get("tags") or [])]
    names = [t["name"] for t in tagged]
    conf = names.index("Deploy dnsmasq.conf")
    logdir = next(i for i, t in enumerate(tagged)
                  if (t.get("ansible.builtin.file") or {}).get("path") == "{{ apply_bang_services_dnsmasq_log_dir }}")
    assert logdir < conf


def test_dnsmasq_tag_ships_the_apparmor_override():
    """The stock profile denies /var/log/dnsmasq/*.log: bang-fiesta's dnsmasq failed with
    EACCES on 2026-11-09 when the conf shipped without the override."""
    tasks = yaml.safe_load(read("roles", "apply_bang_services", "tasks", "main.yml"))
    aa = next(t for t in tasks if (t.get("ansible.builtin.copy") or {}).get("dest")
              == "/etc/apparmor.d/local/usr.sbin.dnsmasq")
    assert "dnsmasq" in (aa.get("tags") or [])


def test_every_dnsmasq_restart_reloads_the_apparmor_profile_first():
    """bang-blues 2026-11-09: override file current, kernel profile stale; a CNAME-only restart
    failed dnsmasq on EACCES. The reload must ride on every restart, not only on override changes."""
    hs = yaml.safe_load(read("roles", "apply_bang_services", "handlers", "main.yml"))
    names = [h["name"] for h in hs]
    reload_ = hs[names.index("reload apparmor dnsmasq profile")]
    assert "restart dnsmasq" in (reload_.get("listen") or [])
    assert names.index("reload apparmor dnsmasq profile") < names.index("restart dnsmasq")
