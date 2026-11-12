"""The TFTP NBPs are BUILT from roles/apply_pxe_payloads/files/ipxe/ (no committed
binary, operator 2026-11-04): pinned commit, patches, embed script, build script."""
import os
import pathlib
import re

import yaml

# the real reaper tree (this overlay links only some of the role's dirs)
REAPER = pathlib.Path(__file__).resolve().parents[1]
ROLE = REAPER / "roles" / "apply_pxe_payloads"
RECIPE = ROLE / "files" / "ipxe"


def test_no_nbp_binary_is_committed_in_the_role():
    assert (ROLE / "files" / "post").is_dir()            # the real role, not an empty path
    assert not list((ROLE / "files").rglob("*.efi"))
    assert not list((ROLE / "files").rglob("*.kpxe"))


def test_recipe_pins_a_full_commit_and_ships_its_inputs():
    conf = (RECIPE / "ipxe.conf").read_text()
    assert re.search(r"^IPXE_COMMIT=[0-9a-f]{40}$", conf, re.M)
    assert re.search(r"^IPXE_REPO=https://", conf, re.M)
    assert (RECIPE / "build-ipxe.sh").read_text().startswith("#!/bin/sh")
    patches = sorted((RECIPE / "patches").glob("*.patch"))
    assert patches and "-\tefi_autoexec_load();" in patches[0].read_text()
    chain = (RECIPE / "chain.ipxe").read_text()
    assert chain.startswith("#!ipxe")
    assert "iseq ${net${a}/bustype} USB && goto defnext ||" in chain
    assert "chain --replace --autofree http://bang/menu.ipxe" in chain


def test_tftp_install_takes_the_built_nbps_not_role_files():
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    tftp = [t for t in tasks if "tftp" in (t.get("tags") or [])]
    names = [t["name"] for t in tftp]
    assert any("Build ipxe.efi" in n for n in names)
    inst = next(t for t in tftp if t["name"].startswith("Install the TFTP NBPs"))
    assert "ipxe_fetch_dir" in inst["ansible.builtin.copy"]["src"]
    assert inst["ansible.builtin.copy"]["backup"] is True
