"""Per-vid iPXE override (operator 2026-10-27): /srv/pxe/v<vid>/override/mac-<mac>.ipxe
is tried before the site-wide /srv/pxe/ipxe/mac-<mac>.ipxe. Overrides are
semi-temporary hand-placed files; the role only creates the directories."""
import os

import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ROLE = os.path.join(_REAPER, "roles", "apply_pxe_payloads")


def read(*p):
    with open(os.path.join(*p)) as f:
        return f.read()


def test_menu_tries_per_vid_override_before_site_wide():
    menu = read(ROLE, "files", "v", "menu.ipxe")
    per_vid = "chain --replace --autofree override/mac-${mac:hexraw}.ipxe ||"
    site = "chain --replace --autofree ${menu-dir}mac-${mac:hexraw}.ipxe ||"
    assert per_vid in menu and site in menu
    assert menu.index(per_vid) < menu.index(site)


def test_role_creates_override_dirs_for_listed_vids_only():
    defaults = yaml.safe_load(read(ROLE, "defaults", "main.yml"))
    assert defaults["apply_pxe_payloads_override_vids"] == []
    tasks = read(ROLE, "tasks", "main.yml")
    assert "/override" in tasks and "apply_pxe_payloads_override_vids" in tasks


def test_lavergne_overrides_on_31_to_33():
    v = yaml.safe_load(read(_REAPER, "inventories", "lavergne", "group_vars", "all", "vars.yml"))
    assert v["apply_pxe_payloads_override_vids"] == [31, 32, 33]


def test_nginx_serves_the_menu_chain_from_the_arrival_vid():
    # boot.ipxe chains http://bang/menu.ipxe; menu.ipxe then fetches boot.ipxe.cfg,
    # ipxe/... and override/... RELATIVE to that URL. Lavergne (2026-10-27) had no
    # hand-placed root copies -> menu.ipxe 404 -> PXE failed. Serve each from
    # /v$vlan first (per-vid override needs it), the root copy second.
    conf = read(_REAPER, "roles", "apply_bang_services", "templates", "nginx-pxe.conf.j2")
    assert "location ~ ^/(menu\\.ipxe|boot\\.ipxe\\.cfg|ipxe/.+|override/.+)$ {" in conf
    assert "try_files /v$vlan$uri $uri =404;" in conf


def test_role_creates_the_shared_ipxe_dir():
    tasks = read(ROLE, "tasks", "main.yml")
    assert 'path: "{{ apply_pxe_payloads_pxe_root }}/ipxe"' in tasks
