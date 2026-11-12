"""scripts/ssh-includes.py (used by ./setup): reaper's Include lines must sit at the
TOP of ~/.ssh/config. An Include after a `Host` line belongs to that Host block in
OpenSSH, so the old append-at-the-end only applied to the last host (2026-11-08)."""
import os
import subprocess
import sys
from pathlib import Path

REAPER = Path(__file__).resolve().parents[1]
SCRIPT = REAPER / "scripts" / "ssh-includes.py"

BEFORE = """Include ~/git/reaper/ssh/braintree
Include ~/git/reaper/ssh/lavergne

Host bang-gouda
  User dbahi

Host turtle-gouda
  HostName 192.168.88.33

Include ~/.ssh/config.d/reaper-jumphosts

Include {home}/git/reaper/ssh/braintree
"""


def run(home, *targets):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([sys.executable, str(SCRIPT), str(home / ".ssh" / "config"), *targets],
                          env=env, capture_output=True, text=True, check=True)


def setup_home(tmp_path):
    (tmp_path / ".ssh").mkdir()
    cfg = tmp_path / ".ssh" / "config"
    cfg.write_text(BEFORE.format(home=tmp_path))
    return cfg


def targets(home):
    return ["~/.ssh/config.d/reaper-jumphosts"] + [f"{home}/git/reaper/ssh/{s}"
                                                   for s in ("braintree", "eindhoven", "lavergne")]


def test_includes_move_to_the_top_before_any_host(tmp_path):
    cfg = setup_home(tmp_path)
    run(tmp_path, *targets(tmp_path))
    lines = cfg.read_text().splitlines()
    first_host = next(i for i, l in enumerate(lines) if l.startswith("Host "))
    incs = [i for i, l in enumerate(lines) if l.startswith("Include ")]
    assert len(incs) == 4 and max(incs) < first_host


def test_tilde_and_absolute_forms_are_one_include(tmp_path):
    cfg = setup_home(tmp_path)
    run(tmp_path, *targets(tmp_path))
    text = cfg.read_text()
    assert text.count("ssh/braintree") == 1 and text.count("ssh/lavergne") == 1


def test_host_blocks_untouched_and_idempotent(tmp_path):
    cfg = setup_home(tmp_path)
    run(tmp_path, *targets(tmp_path))
    once = cfg.read_text()
    assert "Host turtle-gouda\n  HostName 192.168.88.33\n" in once and "Host bang-gouda\n  User dbahi\n" in once
    run(tmp_path, *targets(tmp_path))
    assert cfg.read_text() == once
    assert (tmp_path / ".ssh" / "config.reaper-bak").exists()
