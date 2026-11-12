"""The pinned reaper toolchain (spec 2026-11-07 §3.1): one venv layout on every machine,
built by scripts/toolchain-venv.sh, preferred by the shim, entered with `reaper shell`."""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REAPER = Path(__file__).resolve().parents[1]
SHIM = REAPER / "reaper"
BUILDER = REAPER / "scripts" / "toolchain-venv.sh"
SHARED = Path("/opt/flax/venv/reaper-ansible")


def fake_venv(d: Path) -> Path:
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "python").symlink_to("/usr/bin/python3")
    return d


def shim(args, home, stdin=None, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("REAPER", "VIRTUAL_ENV", "ANSIBLE"))}
    e.update(HOME=str(home), **env)
    return subprocess.run(["bash", str(SHIM), *args], input=stdin, capture_output=True, text=True, env=e,
                          timeout=30)


def envmap(text):
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)


def test_shell_runs_a_command_in_the_toolchain_env(tmp_path):
    v = fake_venv(tmp_path / ".venv-reaper-ansible")
    r = shim(["shell", "env"], tmp_path)
    assert r.returncode == 0, r.stderr
    e = envmap(r.stdout)
    assert e["VIRTUAL_ENV"] == str(v)
    assert e["PATH"].split(":")[:2] == [str(v / "bin"), str(REAPER)]    # `reaper` itself is on PATH
    assert e["ANSIBLE_COLLECTIONS_PATH"] == str(v / "collections")
    assert e["ANSIBLE_CONFIG"] == str(REAPER / "ansible.cfg")
    assert e["ANSIBLE_VAULT_PASSWORD_FILE"] == str(REAPER / "scripts" / "vault-password.sh")
    assert e["REAPER_SHELL"] == "1"


def test_reaper_venv_overrides_home_venv(tmp_path):
    fake_venv(tmp_path / ".venv-reaper-ansible")
    other = fake_venv(tmp_path / "other")
    r = shim(["shell", "env"], tmp_path, REAPER_VENV=str(other))
    assert envmap(r.stdout)["VIRTUAL_ENV"] == str(other)


@pytest.mark.skipif(SHARED.exists(), reason="this machine has the shared bang venv")
def test_shell_without_a_venv_says_how_to_get_one(tmp_path):
    r = shim(["shell", "env"], tmp_path)
    assert r.returncode == 2
    assert str(REAPER / "setup") in r.stderr and "reaper_toolchain" in r.stderr


def test_interactive_shell_marks_prompt_and_deactivate_leaves(tmp_path):
    fake_venv(tmp_path / ".venv-reaper-ansible")
    r = shim(["shell"], tmp_path, stdin='echo "P=$PS1"\ndeactivate\necho never\n')
    assert r.returncode == 0, r.stderr
    assert "P=(reaper) " in r.stdout
    assert "never" not in r.stdout


def _pyver():
    import re
    return re.search(r"^PYVER=([\d.]+)", BUILDER.read_text(), re.M).group(1)


def _stamp():
    """The builder's stamp: the interpreter it targets plus both pin files."""
    return hashlib.sha256(f"python {_pyver()}\n".encode()
                          + (REAPER / "requirements-ansible.txt").read_bytes()
                          + (REAPER / "requirements.yml").read_bytes()).hexdigest()


def _builder(venv, py="/bin/false"):
    return subprocess.run(["bash", str(BUILDER), str(venv)], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "REAPER_TOOLCHAIN_PYTHON": py, "PATH": "/usr/bin:/bin"})


def test_builder_skips_when_pins_unchanged(tmp_path):
    v = fake_venv(tmp_path / "v")
    (v / "bin" / "ansible").write_text("#!/bin/sh\necho 'ansible [core 2.18.3]'\n")
    (v / "bin" / "ansible").chmod(0o755)
    (v / ".reaper-pins").write_text(_stamp() + "\n")
    r = _builder(v)
    assert r.returncode == 0, r.stderr
    assert "up to date" in r.stdout


def test_builder_rebuilds_when_pins_change(tmp_path):
    v = tmp_path / "v"                    # no bin/python: the build must start from `python -m venv`
    v.mkdir()
    (v / ".reaper-pins").write_text("old\n")
    r = _builder(v)                       # python=/bin/false: the rebuild is attempted and fails
    assert r.returncode != 0
    assert (v / ".reaper-pins").read_text() == "old\n"     # a failed build never stamps


def test_builder_rebuilds_a_venv_on_another_interpreter_from_scratch(tmp_path):
    """A venv (it has pyvenv.cfg) whose python is not PYVER is removed and rebuilt, even
    with a current stamp -- never patched in place on the old interpreter (the 3.11 -> 3.12
    move, 2026-11-12). /usr/bin/python3 here is not 3.12.13 (3.14 on the VM, 3.6 on a bang)."""
    v = fake_venv(tmp_path / "v")
    (v / "pyvenv.cfg").write_text("version = x\n")   # no stamp: e.g. a venv built before stamps
    (v / "old-marker").write_text("x")
    r = _builder(v)                       # python=/bin/false: the rebuild is attempted and fails
    assert r.returncode != 0
    assert "rebuilding" in r.stdout and not (v / "old-marker").exists()


def test_builder_never_removes_a_directory_that_is_not_a_venv(tmp_path):
    v = tmp_path / "v"                    # no pyvenv.cfg: not a venv, so nothing is removed
    (v / "bin").mkdir(parents=True)
    (v / "keep").write_text("x")
    _builder(v)
    assert (v / "keep").exists()


def _tasks(role):
    return yaml.safe_load((REAPER / "roles" / role / "tasks" / "main.yml").read_text())


def test_role_stages_pins_and_runs_the_builder():
    tasks = _tasks("apply_reaper_toolchain")
    text = yaml.safe_dump(tasks)
    for f in ("requirements-ansible.txt", "requirements.yml", "toolchain-venv.sh"):
        assert f in text
    [build] = [t for t in tasks if "toolchain-venv.sh" in str(t.get("ansible.builtin.command"))]
    assert "apply_reaper_toolchain_venv" in str(build["ansible.builtin.command"])
    d = yaml.safe_load((REAPER / "roles" / "apply_reaper_toolchain" / "defaults" / "main.yml").read_text())
    assert d["apply_reaper_toolchain_venv"] == str(SHARED)           # = what the shim looks for
    assert str(SHARED) in (REAPER / "scripts" / "toolchain-env.sh").read_text()
    assert "up to date" in str(build["changed_when"])


def test_playbook_imports_role_with_tag():
    pb = yaml.safe_load((REAPER / "playbooks" / "apply_bang_services.yml").read_text())
    [t] = [t for t in pb[0]["tasks"] if (t.get("ansible.builtin.import_role") or {}).get("name")
           == "apply_reaper_toolchain"]
    assert t["tags"] == ["reaper_toolchain"]


def test_setup_builds_the_venv_with_the_builder():
    text = (REAPER / "setup").read_text()
    assert "toolchain-venv.sh" in text
    assert "pip3 install --user ansible" not in text
    assert "ansible-galaxy collection install" not in text     # only the builder installs collections


def test_probe_prefers_the_shared_venv():
    import sys
    sys.path.insert(0, str(REAPER))
    from reapercli.probe_remote import tool
    assert tool("ansible", venv="/opt/flax/venv/reaper-ansible") == "/opt/flax/venv/reaper-ansible/bin/ansible"
    assert tool("ansible", venv="") == "ansible"


def test_doctor_venv_row():
    import sys
    sys.path.insert(0, str(REAPER))
    from reapercli.commands.doctor import venv_row
    assert venv_row("/opt/flax/venv/reaper-ansible", "bang-gouda", "eindhoven").status == "ok"
    bad = venv_row("none (system python3.12 + distro ansible)", "bang-gouda", "eindhoven")
    assert bad.status == "warn" and "reaper_toolchain" in bad.value


def test_shell_is_completed_and_in_usage():
    r = subprocess.run(["bash", str(SHIM), "completion"], capture_output=True, text=True, timeout=30)
    top = [l for l in r.stdout.splitlines() if "COMP_CWORD -eq 1" in l]
    assert top and " shell " in top[0]
    u = subprocess.run(["bash", str(SHIM), "--help"], capture_output=True, text=True, timeout=30)
    assert "shell" in u.stdout + u.stderr


_SSH_LIBS = ("ansible-pylibssh", "paramiko")
_DOCTOR_ROWS = (
    "import json, sys, pathlib\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from reapercli.__main__ import Context\n"
    "from reapercli.commands.doctor import local_rows\n"
    "rows = local_rows(Context(root=pathlib.Path(sys.argv[1]), home=pathlib.Path(sys.argv[2]), hostname='orbit'))\n"
    "print(json.dumps({r.item: [r.status, r.value] for r in rows}))\n")


def test_local_doctor_reads_the_ssh_libraries(tmp_path):
    """doctor reads the network_cli ssh libraries from the python it runs on and
    compares them to requirements-ansible.txt (1d8b555: a venv without them failed
    switch fetch/push at connection load). The test venv deliberately lacks them, so:
    here, a row must tell the truth about this interpreter (missing -> FAIL)."""
    import importlib.metadata as md
    import sys
    sys.path.insert(0, str(REAPER))
    from reapercli.__main__ import Context
    from reapercli.commands.doctor import local_rows
    pins = dict(l.split("==") for l in (REAPER / "requirements-ansible.txt").read_text().split() if "==" in l)
    rows = {r.item: r for r in local_rows(Context(root=REAPER, home=tmp_path, hostname="orbit"))}
    for dist in _SSH_LIBS:
        try:
            have = md.version(dist)
        except md.PackageNotFoundError:
            have = None
        want = "ok" if have == pins[dist] else "FAIL"
        assert rows[dist].status == want, rows[dist].value
        assert (have or "nothing") in rows[dist].value


def test_toolchain_doctor_finds_the_pinned_ssh_libraries(tmp_path):
    """The real check: in the toolchain venv (what `reaper doctor` runs on, chosen
    the same way the shim chooses it), both libraries are installed at their pins."""
    e = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "PYTHON"))}
    r = subprocess.run(["bash", str(SHIM), "shell", "python", "-c", _DOCTOR_ROWS, str(REAPER), str(tmp_path)],
                       capture_output=True, text=True, env=e, timeout=120)
    if r.returncode == 2 and "no toolchain venv" in r.stderr:
        pytest.skip("no reaper toolchain venv on this machine (./setup builds one)")
    assert r.returncode == 0, r.stderr
    import json
    rows = json.loads(r.stdout.splitlines()[-1])
    for dist in _SSH_LIBS:
        assert rows[dist][0] == "ok", rows[dist][1]


def test_ssh_includes_run_on_python312():
    """bare python3 on a bang is 3.6; reaper code runs on python3.12."""
    text = (REAPER / "scripts" / "shell-env.sh").read_text()
    assert 'python3 "$REAPER_DIR/scripts/ssh-includes.py"' not in text
    assert '"$TOOLPY" "$HERE/ssh-includes.py"' in text
    assert 'TOOLPY="${TOOLPY:-python3.12}"' in text


ENVFILE = REAPER / "scripts" / "toolchain-env.sh"


def test_env_script_gives_tools_the_shell_env(tmp_path):
    """VS Code's Ansible extension sources this (ansible.python.activationScript)."""
    v = fake_venv(tmp_path / ".venv-reaper-ansible")
    e = {k: val for k, val in os.environ.items() if not k.startswith(("REAPER", "VIRTUAL_ENV", "ANSIBLE"))}
    e["HOME"] = str(tmp_path)
    r = subprocess.run(["bash", "-c", f'cd /; source "{ENVFILE}" && env'], capture_output=True, text=True,
                       env=e, timeout=30)
    assert r.returncode == 0, r.stderr
    got = envmap(r.stdout)
    want = envmap(shim(["shell", "env"], tmp_path).stdout)
    for k in ("VIRTUAL_ENV", "ANSIBLE_COLLECTIONS_PATH", "ANSIBLE_CONFIG", "ANSIBLE_VAULT_PASSWORD_FILE",
              "REAPER_ROOT"):
        assert got[k] == want[k], k
    assert got["VIRTUAL_ENV"] == str(v)
    assert got["PATH"].split(":")[0] == str(v / "bin")


def test_shim_takes_its_env_from_the_env_script():
    text = SHIM.read_text()
    assert "toolchain-env.sh" in text and ".venv-reaper-ansible" not in text     # one copy of the venv choice


def test_jumphost_ssh_config_sets_no_user():
    """The generated Host blocks name the bang only; `ssh bang-gouda` is then YOU, and
    reaper/ansible say flax themselves (ssh_target, ansible_user)."""
    text = (REAPER / "templates" / "ssh_jumphosts.j2").read_text()
    assert "HostName" in text and "User" not in text


def _run_ssh_section(tmp_path, clone):
    """Run reaper setup's shell step (scripts/shell-env.sh) for a throwaway HOME against a
    fake clone with group-writable ssh/ files."""
    import sys
    return subprocess.run([str(REAPER / "scripts" / "shell-env.sh"), str(clone)],
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, "HOME": str(tmp_path), "TOOLPY": sys.executable})


def _fake_clone(tmp_path):
    clone = tmp_path / "clone"
    (clone / "inventories" / "eindhoven").mkdir(parents=True)
    (clone / "ssh").mkdir()
    (clone / "scripts").mkdir()
    (clone / "scripts" / "ssh-includes.py").write_text((REAPER / "scripts" / "ssh-includes.py").read_text())
    f = clone / "ssh" / "eindhoven"
    f.write_text("Host x\n")
    f.chmod(0o664)                          # a bang clone: umask 0002
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    return clone, f


def test_setup_makes_included_ssh_files_not_group_writable(tmp_path):
    """OpenSSH refuses a group-writable Include: every ssh fails ('Bad owner or permissions')."""
    clone, f = _fake_clone(tmp_path)
    r = _run_ssh_section(tmp_path, clone)
    assert r.returncode == 0, r.stderr
    assert f.stat().st_mode & 0o022 == 0


def test_setup_hooks_keep_ssh_files_safe_after_a_pull(tmp_path):
    clone, f = _fake_clone(tmp_path)
    r = _run_ssh_section(tmp_path, clone)
    assert r.returncode == 0, r.stderr
    for hook in ("post-merge", "post-checkout"):
        f.chmod(0o664)                      # git rewrote it under umask 0002
        h = clone / ".git" / "hooks" / hook
        subprocess.run([str(h)], cwd=clone, check=True)
        assert f.stat().st_mode & 0o022 == 0, hook


def test_setup_includes_the_jump_hosts_and_site_files_at_the_top_of_ssh_config(tmp_path):
    """Bare bang names resolve through the rendered jump hosts (tailscale IPs from the
    vault) only when ~/.ssh/config Includes them -- a bang's own config too."""
    clone, f = _fake_clone(tmp_path)
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "config").write_text("Host mine\n  User me\n")
    r = _run_ssh_section(tmp_path, clone)
    assert r.returncode == 0, r.stderr
    lines = (tmp_path / ".ssh" / "config").read_text().splitlines()
    first_host = next(i for i, l in enumerate(lines) if l.startswith("Host "))
    inc = [i for i, l in enumerate(lines) if l.startswith("Include ")]
    assert any("config.d/reaper-jumphosts" in lines[i] for i in inc)
    assert any(lines[i].endswith(str(f)) for i in inc)
    assert inc and max(inc) < first_host


def test_setup_leaves_a_foreign_hook_alone(tmp_path):
    clone, _ = _fake_clone(tmp_path)
    mine = clone / ".git" / "hooks" / "post-merge"
    mine.write_text("#!/bin/sh\necho mine\n")
    assert _run_ssh_section(tmp_path, clone).returncode == 0
    assert mine.read_text() == "#!/bin/sh\necho mine\n"
