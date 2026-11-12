"""iPXE templates (/srv/pxe/ipxe/templates) ship from reaper, one set for every site.

Lavergne lacked wiwynn-tp-hmc.part, so the rack Deploy step failed with ENOENT
(2026-11-09). The per-host file builders read <platform>.part + <act>.ipxe."""
import os
import re

from flax_post import deploy
from flax_rungs import pxeboot

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ROLE = os.path.join(_REAPER, "roles", "apply_pxe_payloads")
TDIR = os.path.join(ROLE, "files", "ipxe-templates")


def names():
    return set(os.listdir(TDIR))


def test_code_default_platforms_and_act_have_templates():
    want = {f"{pxeboot.PLATFORM}.part", f"{pxeboot.ACT}.ipxe"}
    want |= {f"{p}.part" for p in deploy.DEFAULT_PLATFORMS.values()}
    assert want <= names(), want - names()


def test_no_editor_backups_shipped():
    assert not [n for n in names() if n.endswith("~") or n.startswith(".")]


def test_parts_set_node_setting_and_acts_chain_boot_url():
    for n in names():
        body = open(os.path.join(TDIR, n)).read()
        if n.endswith(".part"):
            assert re.search(r"^set node-setting \S+$", body, re.M), n
        else:
            assert n.endswith(".ipxe") and body.startswith("#!ipxe"), n
            assert "chain --replace --autofree ${boot-url}" in body, n


def test_node_setting_parses_every_part():
    # deploy.node_setting() is what Deploy runs on the platform part
    for n in names():
        if n.endswith(".part"):
            assert deploy.node_setting(open(os.path.join(TDIR, n)).read()), n


def test_retired_templates_are_not_shipped():
    import yaml
    d = yaml.safe_load(open(os.path.join(ROLE, "defaults", "main.yml")))
    retired = set(d["apply_pxe_payloads_ipxe_templates_retired"])
    assert retired == {"rhel-84.ipxe", "sles-15sp2.ipxe", "leap-15.4.ipxe", "leap-15.5.ipxe"}
    assert not retired & names()


def test_role_syncs_without_delete_or_inplace():
    tasks = open(os.path.join(ROLE, "tasks", "main.yml")).read()
    block = tasks[tasks.index("Sync the iPXE templates"):]
    block = block[:block.index("- name:", 1)]
    assert "files/ipxe-templates/" in block and "/ipxe/templates/" in block
    assert "delete" not in block and "inplace" not in block
