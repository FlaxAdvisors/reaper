# tests/test_flax_post_fwd_entity_cache.py
"""flax_post.fwd.entity_cache: best-effort EntityManager cache clear before a
BMC flash. Port of triage's bmc-clear-entity-cache bin -- see module docstring
for why this needs no new mount/credential/image change."""
import json
import subprocess

import pytest

from flax_post.fwd import entity_cache


_CREDS = [
    {"bmcuser": "USERID", "bmcpass": "PASSW0RD"},
    {"bmcuser": "root", "bmcpass": "root-shell-pw"},
    {"bmcuser": "admin", "bmcpass": "admin-pw"},
]


def _write_creds(tmp_path, data):
    p = tmp_path / "credentials-bmc.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_load_root_cred_picks_the_root_entry(tmp_path):
    path = _write_creds(tmp_path, _CREDS)
    assert entity_cache._load_root_cred(path) == ("root", "root-shell-pw")


def test_load_root_cred_none_when_no_root_entry(tmp_path):
    path = _write_creds(tmp_path, [{"bmcuser": "USERID", "bmcpass": "x"}])
    assert entity_cache._load_root_cred(path) is None


def test_load_root_cred_none_on_missing_file(tmp_path):
    assert entity_cache._load_root_cred(str(tmp_path / "nope.json")) is None


def test_load_root_cred_none_on_vault_encrypted_file(tmp_path):
    p = tmp_path / "credentials-bmc.json"
    p.write_text("$ANSIBLE_VAULT;1.1;AES256\ndeadbeef\n")
    assert entity_cache._load_root_cred(str(p)) is None


def test_clear_entity_cache_success(tmp_path, monkeypatch):
    path = _write_creds(tmp_path, _CREDS)
    seen = {}

    def fake_ssh_run(ip, user, password, remote_cmd, timeout=None):
        seen["args"] = (ip, user, password, remote_cmd)
        return "CLEARED\n"

    monkeypatch.setattr(entity_cache, "_ssh_run", fake_ssh_run)
    cleared, reason = entity_cache.clear_entity_cache("172.25.6.101", creds_path=path)
    assert cleared is True and reason == ""
    ip, user, password, remote_cmd = seen["args"]
    assert ip == "172.25.6.101" and user == "root" and password == "root-shell-pw"
    assert "rm -f /var/configuration/system.json" in remote_cmd
    assert "CLEARED" in remote_cmd                       # the load-bearing echo token


def test_clear_entity_cache_no_root_cred(tmp_path):
    path = _write_creds(tmp_path, [{"bmcuser": "USERID", "bmcpass": "x"}])
    cleared, reason = entity_cache.clear_entity_cache("172.25.6.101", creds_path=path)
    assert cleared is False and "no root credential" in reason


def test_clear_entity_cache_ssh_unreachable(tmp_path, monkeypatch):
    path = _write_creds(tmp_path, _CREDS)
    monkeypatch.setattr(entity_cache, "_ssh_run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ssh exited 255")))
    cleared, reason = entity_cache.clear_entity_cache("172.25.6.101", creds_path=path)
    assert cleared is False and reason == "ssh_unreachable"


def test_clear_entity_cache_timeout(tmp_path, monkeypatch):
    path = _write_creds(tmp_path, _CREDS)

    def fake_ssh_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=20)

    monkeypatch.setattr(entity_cache, "_ssh_run", fake_ssh_run)
    cleared, reason = entity_cache.clear_entity_cache("172.25.6.101", creds_path=path)
    assert cleared is False and reason == "ssh_timeout"


def test_clear_entity_cache_rm_failed_when_no_cleared_token(tmp_path, monkeypatch):
    """An empty/unexpected remote output (e.g. the file didn't delete) must not
    be silently treated as success -- the echoed CLEARED token is load-bearing,
    same as triage's bin: an unreachable BMC and a successful no-op must not
    look identical."""
    path = _write_creds(tmp_path, _CREDS)
    monkeypatch.setattr(entity_cache, "_ssh_run", lambda *a, **k: "")
    cleared, reason = entity_cache.clear_entity_cache("172.25.6.101", creds_path=path)
    assert cleared is False and reason == "rm_failed"


def test_clear_entity_cache_never_raises_on_unexpected_error(tmp_path, monkeypatch):
    path = _write_creds(tmp_path, _CREDS)
    monkeypatch.setattr(entity_cache, "_ssh_run",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("weird")))
    cleared, reason = entity_cache.clear_entity_cache("172.25.6.101", creds_path=path)
    assert cleared is False and reason == "ssh_unreachable"


def test_ssh_run_uses_sshpass_env_not_argv(monkeypatch):
    """RULE ZERO: the password must go through SSHPASS env, never -p on the
    command line (which would land in argv/ps and in any error string)."""
    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = b"CLEARED\n"
        stderr = b""

    def fake_run(cmd, timeout=None, stdout=None, stderr=None, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeCompleted()

    monkeypatch.setattr(entity_cache.subprocess, "run", fake_run)
    out = entity_cache._ssh_run("172.25.6.101", "root", "s3kr3t", "echo hi")
    assert out == "CLEARED\n"
    assert "s3kr3t" not in captured["cmd"]                # never in argv
    assert captured["env"]["SSHPASS"] == "s3kr3t"          # only in env
    assert "sshpass" in captured["cmd"] and "-e" in captured["cmd"]


def test_ssh_run_raises_on_nonzero_exit(monkeypatch):
    class _FakeCompleted:
        returncode = 255
        stdout = b""
        stderr = b"Could not resolve hostname"

    monkeypatch.setattr(entity_cache.subprocess, "run",
                        lambda *a, **k: _FakeCompleted())
    with pytest.raises(RuntimeError):
        entity_cache._ssh_run("172.25.6.101", "root", "pw", "echo hi")
