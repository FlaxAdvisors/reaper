"""ssh to a switch/BMC with the password carried in SSHPASS, never in argv."""
import os
import subprocess


class RemoteError(RuntimeError):
    pass


def ssh_run(host: str, user: str, password: str, cmd: str, *,
            runner=subprocess.run, timeout: int = 30) -> str:
    argv = ["sshpass", "-e", "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=8",
            f"{user}@{host}", cmd]
    env = dict(os.environ)
    env["SSHPASS"] = password
    try:
        p = runner(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise RemoteError(f"timeout after {timeout}s: {host}") from e
    except OSError as e:
        raise RemoteError(str(e)) from e
    if p.returncode != 0:
        raise RemoteError((p.stderr or p.stdout or f"rc={p.returncode}").strip())
    return p.stdout
