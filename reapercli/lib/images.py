"""Image build and ship (spec §6; operator decisions 2026-11-07).

build: each site's PRIMARY builds flax-control:<tag> from its own clone, which must
sit at the controller's HEAD (never moved for anyone: they `git pull` there).
ship: primary -> its own standby over the mgmt LAN. Never cross-site."""
import re
import shlex
from pathlib import Path
from typing import Optional

from reapercli.lib.remote import Result, ssh_argv, subprocess_runner

BUILD_CMD = "cd {root} && docker build -t flax-control:{tag} -f flax_control/Dockerfile ."
REMOTE_CLONE = "~/git/reaper"


def version_tag(root: Path) -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "flax_post" / "version.py").read_text())
    return m.group(1) if m else ""


def own_target(bang, on_site: bool, user: str) -> str:
    """The operator's own login, never flax: their clone, and their key, which flax@ on the
    standby accepts (every deploy uses it). The bang aliases default to flax (reaper setup),
    and flax->flax primary->standby is NOT authorized fleet-wide (gouda->edam is denied)."""
    return f"{user}@{bang.mgmt_ip if on_site else bang.name}"


def on_primary(sh, hostname: str, user: str) -> Optional[str]:
    """None = we ARE the primary (run locally); else the operator's ssh target for it."""
    if hostname.split(".")[0] == sh.primary:
        return None
    return own_target(next(b for b in sh.bangs if b.name == sh.primary), sh.on_site, user)


def run_at(target: Optional[str], cmd: str, timeout: float, runner=None) -> Result:
    argv = ["bash", "-c", cmd] if target is None else ssh_argv(target, cmd)
    return (runner or subprocess_runner)(argv, None, timeout)


def ship_cmd(tag: str, standby_ip: str) -> str:
    load = shlex.quote("gunzip | docker load")
    return (f"set -o pipefail; docker save flax-control:{tag} | gzip -1 | "
            f"ssh -o BatchMode=yes -o ConnectTimeout=6 flax@{standby_ip} {load}")


def inspect_cmd(tag: str) -> str:
    return f"docker image inspect flax-control:{tag} >/dev/null 2>&1"
