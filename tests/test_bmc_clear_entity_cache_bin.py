# tests/test_bmc_clear_entity_cache_bin.py
"""scripts/bmc-clear-entity-cache.sh.j2: the rendered bin, driven through its
FLAX_BMC_REMOTE_EXEC hook instead of ssh.

The 2026-09-11 change: after `rm -f /var/configuration/system.json` the bin
restarts xyz.openbmc_project.EntityManager.service in the same ssh session.
EntityManager writes its model back to system.json on its own, so without the
restart a singleton (operator) run was undone within minutes. The pre-flash
callers key on "cleared" only; "restarted" is additive.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REAPER = Path(__file__).resolve().parents[1]
TEMPLATE = REAPER / "scripts" / "bmc-clear-entity-cache.sh.j2"
EM_UNIT = "xyz.openbmc_project.EntityManager.service"

# The fixture "BMC": prints whatever FAKE_OUT holds, exits FAKE_RC, and records
# the remote command it was handed so a test can inspect it.
_FAKE_EXEC = """#!/bin/bash
printf '%s' "$2" > "$FAKE_CMD_FILE"
printf '%b' "$FAKE_OUT"
exit "${FAKE_RC:-0}"
"""


@pytest.fixture
def rendered(tmp_path):
    src = TEMPLATE.read_text()
    assert "{{ bmc_root_password | quote }}" in src
    bin_path = tmp_path / "bmc-clear-entity-cache"
    bin_path.write_text(src.replace("{{ bmc_root_password | quote }}", "'not-a-secret'"))
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR)
    fake = tmp_path / "fake-bmc"
    fake.write_text(_FAKE_EXEC)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return bin_path, fake, tmp_path / "cmd.txt"


def _run(rendered, out, rc):
    bin_path, fake, cmd_file = rendered
    env = dict(os.environ, FLAX_BMC_REMOTE_EXEC=str(fake), FAKE_OUT=out,
               FAKE_RC=str(rc), FAKE_CMD_FILE=str(cmd_file))
    r = subprocess.run([str(bin_path), "clear", "10.0.0.1"], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=30)
    rec = json.loads(r.stdout.strip())
    remote_cmd = cmd_file.read_text() if cmd_file.exists() else ""
    return r.returncode, rec, remote_cmd


def test_remote_command_removes_cache_then_restarts_entity_manager(rendered):
    _, _, remote_cmd = _run(rendered, "CLEARED\\nRESTARTED\\n", 0)
    assert "rm -f /var/configuration/system.json" in remote_cmd
    assert "systemctl restart %s" % EM_UNIT in remote_cmd
    assert "systemctl is-active --quiet %s" % EM_UNIT in remote_cmd
    # rm before restart: the delete must land before EntityManager comes back
    # up and rebuilds, or it would reload the stale cache it is meant to drop.
    assert remote_cmd.index("rm -f") < remote_cmd.index("systemctl restart")


def test_cleared_and_restarted(rendered):
    rc, rec, _ = _run(rendered, "CLEARED\\nRESTARTED\\n", 0)
    assert rc == 0
    assert rec == {"cleared": True, "restarted": True}


def test_cleared_but_restart_failed_reports_both_and_exits_nonzero(rendered):
    """A failed restart makes the remote chain non-zero. That is NOT an
    unreachable BMC: the cache did clear, and the callers that only care about
    that must still see cleared=true."""
    rc, rec, _ = _run(rendered, "CLEARED\\n", 1)
    assert rc == 1
    assert rec == {"cleared": True, "restarted": False}


def test_ssh_unreachable(rendered):
    rc, rec, _ = _run(rendered, "", 255)
    assert rc == 1
    assert rec == {"error": "ssh_unreachable"}


def test_rm_failed_when_no_token_but_clean_exit(rendered):
    rc, rec, _ = _run(rendered, "", 0)
    assert rc == 1
    assert rec == {"error": "rm_failed"}


def test_usage_errors(rendered):
    bin_path, _, _ = rendered
    for argv in ([], ["clear"], ["bogus", "10.0.0.1"]):
        r = subprocess.run([str(bin_path)] + argv, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        assert r.returncode == 2
        assert "usage" in r.stderr
