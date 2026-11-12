"""Running reaper's playbooks for a site (spec §10)."""
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional


def playbook_argv(site, root, playbook: str, *, tags: tuple = (), limit: Optional[str] = None,
                  extra: Optional[dict] = None, check: bool = False, diff: bool = True,
                  configs: Optional[Path] = None) -> list:
    configs = configs or root / "files" / "configs"
    argv = ["ansible-playbook", "-i", str(root / "inventories" / site.name / "hosts.yml"),
            "-e", f"site_name={site.name}", "-e", f"config_output_dir={configs}"]
    for k, v in (extra or {}).items():
        # k=v is split at whitespace by ansible: anything structured or spaced goes as JSON
        simple = isinstance(v, (str, int, float)) and re.fullmatch(r"[\w./:@%+,=~-]*", str(v)) is not None
        argv += ["-e", f"{k}={v}" if simple else json.dumps({k: v})]
    if tags:
        argv += ["--tags", ",".join(tags)]
    if site.dormant_hosts:      # a dormant host is never contacted
        limit = ":".join([limit or "all", *(f"!{h}" for h in site.dormant_hosts)])
    if limit:
        argv += ["--limit", limit]
    if check:
        argv += ["--check", "--diff"] if diff else ["--check"]
    return argv + [str(root / "playbooks" / playbook)]


def _subprocess_play(argv: list, env: dict) -> int:
    # run from the repo root as well: relative paths in ansible.cfg and the playbooks
    return subprocess.run(argv, env=env, cwd=os.path.dirname(env["ANSIBLE_CONFIG"])).returncode


def run_playbook(site, root, playbook: str, *, runner=None, env_extra: Optional[dict] = None, **kw) -> int:
    # reaper's own ansible.cfg wherever reaper is run from (cwd would otherwise pick it)
    env = {**os.environ, "REAPER_SITE": site.name, "ANSIBLE_CONFIG": str(root / "ansible.cfg"),
           **(env_extra or {})}
    return (runner or _subprocess_play)(playbook_argv(site, root, playbook, **kw), env)
