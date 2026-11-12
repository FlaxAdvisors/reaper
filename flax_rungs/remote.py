"""ssh to a switch/BMC with the password carried in SSHPASS, never in argv."""
import os
import subprocess


# ssh's own failure (connect timeout/refused/no route: 255) and our timeout (124): the
# target never answered. sshpass's wrong password (5) or a missing ssh binary here (127)
# is a real failure, not an unreachable target.
UNREACHABLE_RC = (124, 255)


def unreachable(rc: int) -> bool:
    """True when an ssh rc means the command never reached the target."""
    return rc in UNREACHABLE_RC


class RemoteError(RuntimeError):
    def __init__(self, msg, rc=None):
        super().__init__(msg)
        self.rc = rc

    @property
    def unreachable(self) -> bool:
        return self.rc is not None and unreachable(self.rc)


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
        raise RemoteError(f"timeout after {timeout}s: {host}", 124) from e
    except OSError as e:
        raise RemoteError(str(e), 127) from e
    if p.returncode != 0:
        raise RemoteError((p.stderr or p.stdout or f"rc={p.returncode}").strip(), p.returncode)
    return p.stdout


def ssh_cmd(host: str, user: str, password: str, cmd: str, *, runner=subprocess.run,
            timeout: int = 30) -> tuple[int, str, str]:
    """(rc, stdout, stderr) of one read-only remote command. stdin
    is empty: nothing (least of all a password line) is ever fed to the remote shell."""
    argv = ["sshpass", "-e", "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8",
            f"{user}@{host}", cmd]
    env = dict(os.environ)
    env["SSHPASS"] = password
    try:
        p = runner(argv, input="", capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout or "", p.stderr or ""
