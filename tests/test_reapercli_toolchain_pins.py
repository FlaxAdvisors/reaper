"""The repo pins the bangs' ansible toolchain (spec 2026-11-07 §3.1)."""
import os
import pathlib
import re

import yaml

REAPER = pathlib.Path(__file__).resolve().parents[1]

PINS = {
    "ansible.netcommon": "7.1.0",
    "ansible.posix": "1.6.2",
    "ansible.utils": "5.1.2",
    "arista.eos": "10.1.1",
    "cisco.ios": "9.1.1",
    "community.general": "10.4.0",
    "community.network": "5.1.0",
}
FQCN = re.compile(r"\b(ansible\.(?:posix|netcommon|utils)|arista\.eos|cisco\.ios"
                  r"|community\.(?:general|network))\.")


def _collections():
    data = yaml.safe_load((REAPER / "requirements.yml").read_text())
    return {c["name"]: str(c["version"]) for c in data["collections"]}


def test_collections_pinned_exactly():
    assert _collections() == PINS


def test_every_used_collection_is_pinned():
    used = set()
    for pattern in ("roles/**/*.yml", "playbooks/*.yml", "inventories/**/*.yml"):
        for f in REAPER.glob(pattern):
            used.update(FQCN.findall(f.read_text(errors="replace")))
    assert used, "scan found no collection use at all (presence proof)"
    assert used <= set(_collections()), used - set(_collections())


def test_core_pinned():
    lines = (REAPER / "requirements-ansible.txt").read_text().split()
    # exact pin on the 2.18 line (patch bumps are routine; a minor jump is a deliberate step)
    assert [l for l in lines if re.fullmatch(r"ansible-core==2\.18\.\d+", l)]


def test_network_cli_ssh_libraries_pinned():
    # ansible.netcommon.network_cli needs pylibssh (arista: ansible_network_cli_ssh_type
    # libssh) or paramiko; without them every switch fetch/push fails at connection load
    lines = (REAPER / "requirements-ansible.txt").read_text().split()
    assert any(l.startswith("ansible-pylibssh==") for l in lines)
    assert any(l.startswith("paramiko==") for l in lines)
