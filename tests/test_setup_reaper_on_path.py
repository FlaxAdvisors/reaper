"""reaper setup puts `reaper` on PATH (scripts/shell-env.sh link_reaper_on_path): a symlink
in ~/.local/bin or ~/bin to the MAIN clone's shim, never repointed from a worktree or over
another link. The function is lifted out of the script and run under a throwaway HOME."""
import os
import pathlib
import re
import subprocess

import pytest

SETUP = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "shell-env.sh").read_text()


def _fn(name):
    m = re.search(r"^%s\(\) \{\n.*?^\}\n" % name, SETUP, re.S | re.M)
    assert m, name
    return m.group(0)


HARNESS = "\n".join([
    "set -euo pipefail",
    "ok() { echo \"ok $*\"; }; added() { echo \"added $*\"; }; info() { :; }",
    'BASHRC="$HOME/.bashrc"; touch "$BASHRC"',
    _fn("ensure_bashrc_line"), _fn("link_reaper_on_path"),
    "link_reaper_on_path",
])


@pytest.fixture()
def clone(tmp_path):
    c = tmp_path / "git" / "reaper"
    c.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(c)], check=True)
    (c / "reaper").write_text("#!/bin/sh\n")
    subprocess.run(["git", "-C", str(c), "-c", "user.email=t@e", "-c", "user.name=t", "add", "-A"],
                   check=True)
    subprocess.run(["git", "-C", str(c), "-c", "user.email=t@e", "-c", "user.name=t", "commit",
                    "-qm", "x"], check=True)
    return c


def run(home, repo, path):
    env = {"HOME": str(home), "PATH": path, "REAPER_DIR": str(repo)}
    return subprocess.run(["bash", "-c", 'REAPER_DIR="$REAPER_DIR"\n' + HARNESS],
                          env=env, capture_output=True, text=True, check=True).stdout


SYS = "/usr/local/bin:/usr/bin:/bin"


def test_links_into_local_bin_when_it_is_on_path(tmp_path, clone):
    home = tmp_path / "home"
    home.mkdir()
    out = run(home, clone, f"{home}/.local/bin:{SYS}")
    link = home / ".local" / "bin" / "reaper"
    assert link.is_symlink() and os.path.realpath(link) == str(clone / "reaper")
    assert "added" in out
    assert "ok reaper on PATH" in run(home, clone, f"{home}/.local/bin:{SYS}")   # idempotent


def test_uses_home_bin_when_that_is_the_one_on_path(tmp_path, clone):
    home = tmp_path / "home"
    home.mkdir()
    run(home, clone, f"{home}/bin:{SYS}")
    assert (home / "bin" / "reaper").is_symlink()
    assert not (home / ".local" / "bin" / "reaper").exists()


def test_neither_on_path_adds_local_bin_to_bashrc(tmp_path, clone):
    home = tmp_path / "home"
    home.mkdir()
    run(home, clone, SYS)
    assert (home / ".local" / "bin" / "reaper").is_symlink()
    assert 'export PATH="$HOME/.local/bin:$PATH"' in (home / ".bashrc").read_text()


def test_a_link_to_another_clone_is_left_alone(tmp_path, clone):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    other = tmp_path / "other-reaper"
    other.write_text("#!/bin/sh\n")
    (home / ".local" / "bin" / "reaper").symlink_to(other)
    out = run(home, clone, f"{home}/.local/bin:{SYS}")
    assert os.path.realpath(home / ".local" / "bin" / "reaper") == str(other)
    assert "left alone" in out


def test_a_worktree_never_repoints_it(tmp_path, clone):
    home = tmp_path / "home"
    home.mkdir()
    wt = tmp_path / "git" / "reaper.feature"
    subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach", str(wt)],
                   check=True)
    out = run(home, wt, f"{home}/.local/bin:{SYS}")
    assert not (home / ".local" / "bin" / "reaper").exists()
    assert "skipped in a worktree" in out
