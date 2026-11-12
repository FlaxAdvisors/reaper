"""FOLLOWUP-20261109 item 5: a firmware manifest written by ansible (template/copy:
rename, new inode) left the containers that bind-mount it as a single file on the old
copy, and the post drivers load theirs once at start. Every manifest write now notifies
a handler that restarts exactly its readers when the file changed."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _tasks(role, f="main.yml"):
    return yaml.safe_load((ROOT / f"roles/{role}/tasks/{f}").read_text())


def _handlers(role):
    return yaml.safe_load((ROOT / f"roles/{role}/handlers/main.yml").read_text())


def _notify(tasks, name):
    [t] = [t for t in tasks if t["name"] == name]
    n = t.get("notify") or []
    return [n] if isinstance(n, str) else n


def _handler_text(role, name):
    [h] = [h for h in _handlers(role) if h["name"] == name]
    return str(h)


def test_bmc_manifest_restarts_the_console_and_the_two_post_units():
    assert _notify(_tasks("apply_bmc_fw_manifest"), "Render BMC firmware version manifest") == [
        "Restart BMC manifest readers"]
    h = _handler_text("apply_bmc_fw_manifest", "Restart BMC manifest readers")
    assert "try-restart flax-post.service flax-post-fwd.service" in h
    assert "docker restart flax-stack-flax-control-1" in h


def test_host_manifest_restarts_the_console():
    assert _notify(_tasks("apply_host_fw_manifest"), "Render host (BIOS) firmware version manifest") == [
        "Restart host manifest readers"]
    assert "docker restart flax-stack-flax-control-1" in _handler_text(
        "apply_host_fw_manifest", "Restart host manifest readers")


def test_post_bios_and_nic_manifests_restart_their_drivers():
    t = _tasks("apply_flax_post", "systemd.yml")
    assert "Restart flax-post-biosd" in _notify(t, "Deploy BIOS firmware version manifest")
    assert "Restart flax-post-nicd" in _notify(t, "Deploy nic-firmware-versions.json")
