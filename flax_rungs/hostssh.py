"""Run a script on a live-image host as flax under sudo; password only via SSHPASS."""
import os
import subprocess
import time


def run(host_ip: str, user: str, password: str, script: str, *, timeout: int = 60,
        runner=subprocess.run) -> tuple[int, str]:
    argv = ["sshpass", "-e", "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=8",
            f"{user}@{host_ip}", "sudo", "-n", "bash", "-s"]
    env = dict(os.environ)
    env["SSHPASS"] = password
    try:
        p = runner(argv, input=script, capture_output=True, text=True,
                   timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except OSError as e:
        return 127, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def wait_ssh(host_ip: str, user: str, password: str, *, deadline_s: int = 900,
             poll_s: int = 15, last: dict | None = None, runner=subprocess.run,
             sleep=time.sleep, clock=time.monotonic) -> bool:
    """Bounded: True once `sudo true` works over ssh, False at the deadline."""
    end = clock() + deadline_s
    while True:
        rc, out = run(host_ip, user, password, "true", timeout=20, runner=runner)
        if rc == 0:
            return True
        if last is not None:
            last["ssh"] = f"rc={rc}"
        if clock() + poll_s >= end:
            return False
        sleep(poll_s)
