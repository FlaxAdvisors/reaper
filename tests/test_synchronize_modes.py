"""A synchronize that copies modes (perms: true) must not copy the DEPLOYING clone's umask:
bang clones are 0002 (664/775), orbit is 0022 (644/755), so every deploy from the other
kind of clone flipped the modes, reported changed, and restarted the service (flax-post-deployd,
2026-11-08). --chmod=go-w (or a full D/F pin) makes the target mode independent of the clone."""
import os
from pathlib import Path

import yaml

REAPER = Path(__file__).resolve().parents[1]


def _syncs():
    for f in sorted(REAPER.glob("roles/*/tasks/*.yml")):
        for t in yaml.safe_load(f.read_text()) or []:
            if isinstance(t, dict) and "ansible.posix.synchronize" in t:
                yield f"{f.parent.parent.name}: {t['name']}", t["ansible.posix.synchronize"]


def test_found_the_syncs():
    assert len(list(_syncs())) >= 5          # presence proof: the scan sees the tasks


def test_perms_syncs_pin_modes():
    bad = [name for name, s in _syncs() if s.get("perms") and "--chmod=" not in str(s.get("rsync_opts"))]
    assert bad == []
