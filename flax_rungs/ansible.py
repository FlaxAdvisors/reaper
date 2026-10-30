"""flax_user: run the bang's /opt/flax/ansible/flax_user.yml against the installed OS.

A per-run inventory goes into the caller's work dir (the order dir): `[nodes]` = the
host IPs, ansible_user=flax. The password reaches ansible only through
`--extra-vars @<file>`: a 0600 JSON file in the same dir, removed in a finally (pass,
fail, timeout, exception); it is never in argv or the environment. ansible-playbook
runs from the playbook's dir so its ansible.cfg applies (forks, callbacks); extra-vars
outrank the playbook's group_vars, so the vaulted password, not the one in
group_vars/nodes.yml, is used.

Per host the PLAY RECAP decides: unreachable>0 -> unmeasured (ssh never answered:
nothing judged; but a refused login -- ansible calls that UNREACHABLE too -- fails,
naming the user), failed>0 -> fail, else pass. A host missing from the recap (the
playbook died first) fails. Every device's artifact is the full output, with the
password scrubbed."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .result import UNMEASURED, RungResult

PLAYBOOK = "/opt/flax/ansible/flax_user.yml"
INVENTORY = "flax_user-inventory.ini"
ARTIFACT = "ansible-playbook.txt"
TAIL = 600

_AUTH = re.compile(r"password|Permission denied|Authentication fail", re.I)
_RECAP_LINE = re.compile(r"^(\S+)\s*:\s*((?:\w+=\d+\s*)+)$")


def inventory(hosts: dict[str, str], user: str) -> str:
    """INI inventory text: one `[nodes]` line per host IP (pos as a comment var)."""
    lines = ["[nodes]"]
    for pos, ip in hosts.items():
        lines.append(f"{ip} ansible_user={user} rackpost_pos={pos}")
    return "\n".join(lines) + "\n"


def parse_recap(text: str) -> dict[str, dict[str, int]]:
    """host -> {ok, changed, unreachable, failed, skipped, rescued, ignored} from the
    last PLAY RECAP block of ansible-playbook's output."""
    out: dict[str, dict[str, int]] = {}
    lines = text.splitlines()
    start = max((i for i, ln in enumerate(lines) if ln.startswith("PLAY RECAP")), default=None)
    if start is None:
        return out
    for ln in lines[start + 1:]:
        m = _RECAP_LINE.match(ln.strip())
        if not m:
            if ln.strip():
                break
            continue
        out[m.group(1)] = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", m.group(2))}
    return out


def _auth_refused(text: str, ip: str) -> bool:
    """ansible reports a refused login as UNREACHABLE: that is a real failure (the
    host answered), not an unmeasured one."""
    tag = f"[{ip}]: UNREACHABLE!"
    return any(tag in ln and _AUTH.search(ln) for ln in text.splitlines())


def _scrub(text: str, pw: str) -> str:
    return text.replace(pw, "<redacted>") if pw else text


def _tail(text: str) -> str:
    return text.strip()[-TAIL:]


def _all(hosts, verdict, why, arts) -> dict[str, RungResult]:
    extra = {"reason": why} if verdict == UNMEASURED else {}
    return {pos: RungResult(verdict, {"headline": why, "error": why, "host": ip, **extra},
                            dict(arts))
            for pos, ip in hosts.items()}


def flax_user(hosts: dict[str, str], user: str, pw: str, workdir, *,
              playbook: str = PLAYBOOK, timeout: int = 1800,
              runner=subprocess.run) -> dict[str, RungResult]:
    """hosts: pos -> host IP. One RungResult per pos."""
    if not hosts:
        return {}
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    inv = work / INVENTORY
    inv.write_text(inventory(hosts, user))
    fd, secret = tempfile.mkstemp(prefix=".flax_user-", suffix=".json", dir=work)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"ansible_user": user, "ansible_password": pw,
                       "ansible_become_password": pw}, f)
        argv = ["ansible-playbook", playbook, "-i", str(inv), "--extra-vars", f"@{secret}"]
        env = dict(os.environ, ANSIBLE_HOST_KEY_CHECKING="False", ANSIBLE_NOCOLOR="1",
                   ANSIBLE_FORCE_COLOR="0", ANSIBLE_RETRY_FILES_ENABLED="False")
        try:
            p = runner(argv, capture_output=True, text=True, timeout=timeout, env=env,
                       cwd=str(Path(playbook).parent))
        except subprocess.TimeoutExpired as e:
            text = _scrub(((e.stdout or "") if isinstance(e.stdout, str) else "")
                          + ((e.stderr or "") if isinstance(e.stderr, str) else ""), pw)
            return _all(hosts, "fail", f"ansible-playbook timed out after {timeout}s",
                        {ARTIFACT: text} if text else {})
        except OSError as e:
            return _all(hosts, "fail", f"ansible-playbook did not run: {e}", {})
    finally:
        try:
            os.unlink(secret)
        except FileNotFoundError:
            pass
    text = _scrub((p.stdout or "") + (("\n" + p.stderr) if p.stderr else ""), pw)
    arts = {ARTIFACT: text}
    recap = parse_recap(text)
    out = {}
    for pos, ip in hosts.items():
        r = recap.get(ip)
        if r is None:
            why = f"{ip} not in PLAY RECAP (ansible-playbook rc={p.returncode})"
            out[pos] = RungResult("fail", {"headline": why, "error": why, "host": ip,
                                           "tail": _tail(text)}, dict(arts))
            continue
        rows = [[k, str(r.get(k, 0))] for k in
                ("ok", "changed", "unreachable", "failed", "skipped", "rescued", "ignored")]
        det = {"host": ip, "recap": r, "rc": p.returncode, "rows": rows,
               "notes": [f"ansible-playbook {playbook} as {user}"]}
        if r.get("unreachable", 0) and _auth_refused(text, ip):
            why = f"{ip} refused ssh login as {user} (password/permission denied)"
            out[pos] = RungResult("fail", {**det, "headline": why, "error": why,
                                           "tail": _tail(text)}, dict(arts))
        elif r.get("unreachable", 0):
            why = f"{ip} unreachable for ansible (ssh as {user})"
            out[pos] = RungResult(UNMEASURED, {**det, "headline": why, "reason": why,
                                               "error": why, "tail": _tail(text)}, dict(arts))
        elif r.get("failed", 0):
            why = f"{r['failed']} task(s) failed on {ip}"
            out[pos] = RungResult("fail", {**det, "headline": why, "error": why,
                                           "tail": _tail(text)}, dict(arts))
        else:
            out[pos] = RungResult("pass", {**det, "headline":
                                           f"ok={r.get('ok', 0)} changed={r.get('changed', 0)}"},
                                  dict(arts))
    return out
