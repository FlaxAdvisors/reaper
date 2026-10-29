"""Run a script on a live-image host as flax under sudo; password only via SSHPASS.

run() returns (rc, stdout, stderr) separately: callers parse stdout only (ssh's
"Warning: Permanently added ..." or a sudo hostname complaint lands on stderr and
must never corrupt JSON/markers), and put stderr only into error text/details."""
import os
import subprocess


def run(host_ip: str, user: str, password: str, script: str, *, timeout: int = 60,
        runner=subprocess.run) -> tuple[int, str, str]:
    argv = ["sshpass", "-e", "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8",
            f"{user}@{host_ip}", "sudo", "-n", "bash", "-s"]
    env = dict(os.environ)
    env["SSHPASS"] = password
    try:
        p = runner(argv, input=script, capture_output=True, text=True,
                   timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout or "", p.stderr or ""


def tail(out: str, err: str, n: int = 400) -> str:
    """The last n chars of stdout+stderr, for error details only (never parsed)."""
    return ((out or "") + (err or "")).strip()[-n:]
