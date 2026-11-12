"""ssh to bangs: one bounded call per bang, all bangs in parallel (spec §10)."""
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Result:
    rc: int
    out: str
    err: str


Runner = Callable[[list, Optional[str], float], Result]

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ControlMaster=auto",
            "-o", "ControlPath=~/.ssh/cm-reaper-%r@%h:%p", "-o", "ControlPersist=60",
            "-o", "StrictHostKeyChecking=accept-new"]


def ssh_argv(target: str, remote_cmd: str) -> list:
    return ["ssh", *SSH_OPTS, target, remote_cmd]


def subprocess_runner(argv: list, stdin: Optional[str], timeout: float) -> Result:
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
        return Result(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return Result(124, "", "timeout")


def fanout(targets: dict, remote_cmd: str, stdin: Optional[str], timeout: float,
           runner: Optional[Runner] = None) -> dict:
    run = runner or subprocess_runner
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {name: pool.submit(run, ssh_argv(t, remote_cmd), stdin, timeout)
                   for name, t in targets.items()}
        return {name: f.result() for name, f in futures.items()}
