"""Deploy guards (spec §8.2). Each refuses with `Refused`; main() exits 3 and says why."""
import subprocess
import sys
from pathlib import Path
from typing import Optional


class Refused(Exception):
    """A guard said no. Nothing has been changed."""


def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


FETCH_TIMEOUT = 20


def _fetch(root: Path, branch: str) -> bool:
    """Refresh origin/<branch> so 'behind' means behind origin as it is NOW, not as of the
    clone's last fetch (a stale ref let `reaper fw` commit on a clone that was behind, and
    the push then failed: 2026-11-19). False when origin could not be reached in time."""
    try:
        r = subprocess.run(["git", "-C", str(root), "fetch", "-q", "origin", branch],
                           capture_output=True, text=True, timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def clone_guard(root: Path, allow_branch: Optional[str] = None) -> str:
    """HEAD's sha, if the clone is clean, on main (or the named branch), pushed and not
    behind origin. Fetches first; an unreachable origin warns and checks the last-fetched ref."""
    dirty = [l for l in _git(root, "status", "--porcelain").stdout.splitlines() if l.strip()]
    if dirty:
        raise Refused(f"clone dirty: {len(dirty)} paths ({root})")
    branch = _git(root, "branch", "--show-current").stdout.strip()
    if branch != "main" and branch != allow_branch:
        raise Refused(f"branch {branch or '(detached)'} is not main: pass --branch {branch} to use it deliberately")
    if not _fetch(root, branch):
        print(f"warn  git fetch origin {branch} failed: checking against the last-fetched origin/{branch}",
              file=sys.stderr)
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    behind = _git(root, "rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip()
    if _git(root, "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}").returncode != 0:
        if behind not in ("", "0"):
            raise Refused(f"HEAD {head[:7]} has diverged from origin/{branch} ({behind} commits behind): "
                          f"git pull --rebase, then push")
        raise Refused(f"HEAD {head[:7]} not on origin/{branch}: push first")
    if behind not in ("", "0"):     # a partial pull: deploying would roll back newer work
        raise Refused(f"HEAD {head[:7]} is {behind} commits behind origin/{branch}: git pull first")
    return head


def missing_images(view, comps: list) -> list:
    """'<bang>: <repo>:<tag>' for every pinned image of `comps` absent on a live bang it is
    placed on. Placement, not the VIP: the standby needs the image for a failover."""
    out = []
    for c in comps:
        if not (c.image_repo and c.image_var in view.pins):
            continue
        img = f"{c.image_repo}:{view.pins[c.image_var]}"
        for bang in view.placed.get(c.name, ()):
            f = view.facts.get(bang, {})
            if f.get("dormant"):
                continue
            line = f"{bang}: {img}"
            if img not in (f.get("images") or []) and line not in out:
                out.append(line)
    return out
