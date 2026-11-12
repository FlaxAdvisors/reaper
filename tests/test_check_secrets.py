"""scripts/check-secrets: the rule-zero git hooks (pre-commit, pre-push) against a
throwaway repo. Secret-shaped strings are assembled at runtime so this file itself
passes `check-secrets tree`."""
import os
import pathlib
import subprocess
import sys

import pytest

CHECK = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check-secrets"
HASH6 = "$" + "6$" + "saltsalt$" + "a" * 86
KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
VAULTED = "$ANSIBLE_VAULT;1.1;AES256\n6162636465\n"


def git(repo, *args, **kw):
    return subprocess.run(("git", "-C", str(repo)) + args, check=True, capture_output=True,
                          text=True, **kw).stdout


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "README").write_text("x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


def stage(repo, path, content):
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode())
    git(repo, "add", "-A")


def run(repo, *args, stdin=""):
    return subprocess.run([sys.executable, str(CHECK)] + list(args), cwd=repo, input=stdin,
                          capture_output=True, text=True)


@pytest.mark.parametrize("path,content", [
    ("files/configs/eindhoven/services/credentials.json", '{"bmc": "hunter2"}\n'),
    ("inventories/eindhoven/group_vars/all/vault.yml", "vault_x: hunter2\n"),
    (".vault_pass_eindhoven", "pw\n"),
    ("tests/fixtures/blob.xlsx", b"PK\x03\x04\x00\x00binary"),
    ("roles/x/files/a.sh", "ssh-keygen\n" + KEY + "\n"),
    ("roles/x/files/a.cfg", "rootpw --iscrypted " + HASH6 + "\n"),
    ("scripts/a.sh", "sshpass -p s3cretpw ssh host\n"),  # secrets-ok: fake
    ("scripts/b.sh", "ipmitool -I lanplus -H h -U admin -P s3cretpw power on\n"),  # secrets-ok: fake
])
def test_pre_commit_blocks(repo, path, content):
    stage(repo, path, content)
    r = run(repo, "pre-commit")
    assert r.returncode == 1 and path in r.stderr, r.stderr


@pytest.mark.parametrize("path,content", [
    ("files/configs/eindhoven/services/credentials.json", VAULTED),
    ("inventories/eindhoven/group_vars/all/vault.yml", VAULTED),
    ("scripts/a.sh", 'sshpass -p "$PW" ssh host\nipmitool -H h -U "$u" -P "$pw" power on\n'),
    ("roles/x/templates/a.j2", "ipmitool -U admin -P {{ vault_bmc_password }} power on\n"),
    ("tests/test_x.py", 'H = "$6$abc$def"\n'),                       # short fake, not a hash
    ("tests/test_y.py", "H = '" + HASH6 + "'  # secrets-ok: fake\n"),
    ("files/configs/eindhoven/rabbit-gouda/config/running-config.txt",
     "username admin role network-admin secret sha512 " + HASH6 + "\n"),
])
def test_pre_commit_allows(repo, path, content):
    stage(repo, path, content)
    r = run(repo, "pre-commit")
    assert r.returncode == 0, r.stderr


def test_only_added_lines_are_judged(repo):
    """A pre-existing literal does not block an unrelated edit to the same file."""
    stage(repo, "scripts/a.sh", "ipmitool -U admin -P admin chassis status\n")  # secrets-ok: fake
    git(repo, "commit", "-q", "--no-verify", "-m", "legacy")
    stage(repo, "scripts/a.sh", "ipmitool -U admin -P admin chassis status\necho ok\n")  # secrets-ok: fake
    assert run(repo, "pre-commit").returncode == 0


def test_pre_push_checks_every_commit_being_pushed(repo, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "fetch", "-q", "origin")
    stage(repo, "files/configs/x/services/credentials.json", '{"pw": "hunter2"}\n')
    git(repo, "commit", "-q", "--no-verify", "-m", "oops")
    stage(repo, "README", "y\n")                       # a later, clean commit on top
    git(repo, "commit", "-q", "-m", "clean")
    head = git(repo, "rev-parse", "HEAD").strip()
    base = git(repo, "rev-parse", "origin/main").strip()
    r = run(repo, "pre-push", stdin="refs/heads/main %s refs/heads/main %s\n" % (head, base))
    assert r.returncode == 1 and "credentials.json" in r.stderr
    r = run(repo, "pre-push", stdin="refs/heads/new %s refs/heads/new %s\n" % (head, "0" * 40))
    assert r.returncode == 1, "a new branch is checked against what no remote has yet"
    assert run(repo, "pre-push", stdin="(delete) %s refs/heads/old %s\n" % ("0" * 40, base)
               ).returncode == 0


def test_installed_hook_blocks_a_real_commit(repo):
    """End to end: the installer's pre-commit stub runs this checker on `git commit`."""
    root = CHECK.parents[1]
    (repo / "scripts").mkdir()
    os.symlink(CHECK, repo / "scripts" / "check-secrets")
    os.symlink(root / "scripts" / "install-git-hooks.sh", repo / "scripts" / "install-git-hooks.sh")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tooling")
    subprocess.run([str(repo / "scripts" / "install-git-hooks.sh")], check=True)
    stage(repo, "inventories/x/group_vars/all/vault.yml", "vault_x: hunter2\n")
    r = subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "leak"],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "vault.yml" in r.stderr


def test_ruled_vendor_default_passes_only_in_its_exact_form(repo):
    """The AMI admin/admin probe in bmc-enable-inband-admin.sh.j2 is operator-ruled
    (2026-11-11); any other literal in the same file still blocks."""
    path = "scripts/bmc-enable-inband-admin.sh.j2"
    stage(repo, path, 'if ipmitool -C 17 -I lanplus -H "$bmc" -U admin -P admin \\\n')  # secrets-ok: ruled
    assert run(repo, "pre-commit").returncode == 0
    stage(repo, path, 'if ipmitool -C 17 -I lanplus -H "$bmc" -U admin -P admin \\\n'  # secrets-ok: ruled
                      'ipmitool -H "$bmc" -U root -P s3cretpw power on\n')  # secrets-ok: fake
    r = run(repo, "pre-commit")
    assert r.returncode == 1 and path + ":2" in r.stderr, r.stderr


def test_hook_fails_closed_without_python312(repo, tmp_path):
    """No python3.12 on PATH: the commit is BLOCKED with a pointer to ./setup, never
    let through unchecked (rule zero must not silently skip)."""
    import shutil
    root = CHECK.parents[1]
    (repo / "scripts").mkdir()
    os.symlink(CHECK, repo / "scripts" / "check-secrets")
    os.symlink(root / "scripts" / "install-git-hooks.sh", repo / "scripts" / "install-git-hooks.sh")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", "tooling")
    subprocess.run([str(repo / "scripts" / "install-git-hooks.sh")], check=True)
    bare = tmp_path / "bin"                      # a PATH with git and sh but no python3.12
    bare.mkdir()
    for tool in ("git", "sh"):
        os.symlink(shutil.which(tool), bare / tool)
    stage(repo, "README", "clean change\n")
    r = subprocess.run([shutil.which("git"), "-C", str(repo), "commit", "-q", "-m", "x"],
                       capture_output=True, text=True, env=dict(os.environ, PATH=str(bare)))
    assert r.returncode != 0 and "needs python3.12" in r.stderr, r.stderr
