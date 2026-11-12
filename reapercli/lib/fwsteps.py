"""reaper fw: the bang-side steps (spec 2026-11-11 §6). Every read of the tar happens on
the bang over ssh; the VM only sees sha256 sums and the MANIFEST text. The standby copy
goes bang to bang (holder -> standby over the mgmt LAN as flax), never through the VM."""
import json
import posixpath
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from reapercli.commands.svc_restart import _recreate
from reapercli.lib.components import compose_argv
from reapercli.lib.fwdrain import on_cmd
from reapercli.lib.fwentry import LANES
from reapercli.lib.guards import Refused
from reapercli.lib.inventory import ssh_target
from reapercli.lib.remote import fanout

SHARE = "/export/share"
CLEAN_TIMEOUT = 300           # ssh side; the script itself stops writing at CLEAN_DEADLINE
CLEAN_DEADLINE = 180
API = "prod-api-1"
_SCRIPT = Path(__file__).resolve().parent.parent / "fwreset_remote.py"
_ENV_PREFIX = {"bmc_fw": "BMC_FW_", "bios_fw": "BIOS_FW_"}
_ENV_SHOWN = ("MODE", "ENABLE_PORTS", "DISABLE_PORTS")


@dataclass(frozen=True)
class TarInfo:
    sha256: str
    manifest_text: str


def artifact_rel(arg: str) -> str:
    a = (arg or "").strip()
    if a.startswith("/"):
        n = posixpath.normpath(a)
        if not n.startswith(SHARE + "/"):
            raise Refused(f"{arg}: not under {SHARE} (the agents fetch the image from the share)")
        rel = n[len(SHARE) + 1:]
    else:
        rel = posixpath.normpath(a) if a else ""
    if not rel or rel == "." or rel.startswith("..") or "/../" in f"/{rel}/":
        raise Refused(f"{arg!r}: not a path to a file on the share")
    return rel


def _one(sh, bang: str, cmd: str, runner, stdin: Optional[str] = None, timeout: float = 60):
    b = next(x for x in sh.bangs if x.name == bang)
    return fanout({bang: ssh_target(b, sh.on_site)}, cmd, stdin, timeout, runner)[bang]


def read_tar(sh, bang: str, rel: str, runner) -> TarInfo:
    p = shlex.quote(f"{SHARE}/{rel}")
    r = _one(sh, bang, f"test -r {p} || exit 3; sha256sum {p} && echo --MANIFEST-- && tar -xOf {p} MANIFEST",
             runner, timeout=180)
    if r.rc == 3:
        raise Refused(f"{SHARE}/{rel} not on {bang}")
    if r.rc or "--MANIFEST--" not in r.out:
        raise Refused(f"{bang}: cannot read {SHARE}/{rel}: {(r.err or r.out).strip()[:200]}")
    head, _, manifest = r.out.partition("--MANIFEST--\n")
    return TarInfo(head.split()[0], manifest)


def remote_sha(sh, bang: str, rel: str, runner) -> Optional[str]:
    p = shlex.quote(f"{SHARE}/{rel}")
    r = _one(sh, bang, f"test -r {p} && sha256sum {p}", runner, timeout=180)
    return r.out.split()[0] if r.rc == 0 and r.out.strip() else None


def sync_cmd(rel: str, standby_ip: str) -> str:
    """Run on the holder: copy the tar to the standby (same path), then print its sha256 there."""
    p = f"{SHARE}/{rel}"
    ssh = f"ssh -o BatchMode=yes -o ConnectTimeout=6 flax@{standby_ip}"
    return (f"set -e; {ssh} mkdir -p {shlex.quote(posixpath.dirname(p))}; "
            f"rsync -a -e {shlex.quote(ssh.rsplit(' ', 1)[0])} {shlex.quote(p)} "
            f"flax@{standby_ip}:{shlex.quote(p)}")


def sync(sh, holder: str, standby_ip: str, rel: str, runner) -> int:
    return _one(sh, holder, sync_cmd(rel, standby_ip), runner, timeout=1800).rc


def rendered_target(sh, bang: str, lane: str, family: str, runner) -> Optional[str]:
    r = _one(sh, bang, f"cat {LANES[lane].manifest}", runner)
    try:
        return (json.loads(r.out).get(family) or {}).get("target_version") if r.rc == 0 else None
    except ValueError:
        return None


def agent_env(sh, bang: str, agent: str, runner) -> dict:
    """{"running": bool, MODE/ENABLE_PORTS/DISABLE_PORTS, SWITCHPORTRECON_URL} of prod-<agent>-1."""
    fmt = "{{.State.Status}}{{println}}image={{.Config.Image}}{{println}}{{range .Config.Env}}{{println .}}{{end}}"
    r = _one(sh, bang, f"docker inspect prod-{agent}-1 --format {shlex.quote(fmt)}", runner)
    if r.rc or not r.out.strip():
        return {"running": False}
    lines = r.out.splitlines()
    out = {"running": lines[0].strip() == "running", "env": {}}
    pre = _ENV_PREFIX[agent]
    for line in lines[1:]:
        k, sep, v = line.partition("=")
        if not sep:
            continue
        if k == "image":
            out["image"] = v
        elif k.startswith(pre):
            out["env"][k] = v
            if k[len(pre):] in _ENV_SHOWN:
                out[k[len(pre):]] = v
        elif k == "SWITCHPORTRECON_URL":
            out[k] = v
    return out


def compose_service(sh, bang: str, comp, agent: str, runner) -> dict:
    """{"image", "env"} the next `up --force-recreate <agent>` would use (compose config)."""
    cmd = "sudo -n bash -c " + shlex.quote(compose_argv(comp.compose, "config", "--format", "json"))
    r = _one(sh, bang, cmd, runner)
    try:
        svc = json.loads(r.out)["services"][agent]
    except (ValueError, KeyError, TypeError):
        raise Refused(f"{bang}: cannot read the triage compose config for {agent}: {r.err.strip()[-200:]}")
    pre = _ENV_PREFIX[agent]
    env = svc.get("environment") or {}
    return {"image": svc.get("image", ""), "env": {k: v for k, v in env.items() if k.startswith(pre)}}


def env_drift(live: dict, cfg: dict) -> list:
    """What a recreate would change: the agent's own env vars and its image."""
    out = []
    if live.get("image") != cfg.get("image"):
        out.append(f"image: running {live.get('image')}, recreate would use {cfg.get('image')}")
    le, ce = live.get("env") or {}, cfg.get("env") or {}
    for k in sorted(set(le) | set(ce)):
        if le.get(k) != ce.get(k):
            out.append(f"{k}: running {le.get(k, '(unset)')}, recreate would use {ce.get(k, '(unset)')}")
    return out


_POST_UNITS = {"bmc": ("flax-post", "flax-post-fwd"), "bios": ("flax-post-biosd",)}
_READERS = ("agent", "flax-post", "flax-post-fwd", "flax-post-biosd", "console")
CONSOLE = "flax-stack-flax-control-1"


def reader_ages(sh, bang: str, lane: str, runner) -> dict:
    """Epoch seconds: the rendered manifest(s)' mtime and each RUNNING reader's start."""
    lp = LANES[lane]
    parts = [f"echo manifest $(stat -c %Y {lp.manifest})"]
    if lane == "bios":
        parts.append("echo post_manifest $(stat -c %Y /etc/flax/bios-firmware-versions.json)")
    for key, name in (("agent", f"prod-{lp.agent}-1"), ("console", CONSOLE)):
        parts.append(f"[ \"$(docker inspect -f '{{{{.State.Running}}}}' {name} 2>/dev/null)\" = true ] && "
                     f"echo {key} $(date -d \"$(docker inspect -f '{{{{.State.StartedAt}}}}' {name})\" +%s)")
    for u in _POST_UNITS[lane]:
        parts.append(f"systemctl is-active -q {u} && "
                     f"echo {u} $(date -d \"$(systemctl show -p ActiveEnterTimestamp --value {u})\" +%s)")
    r = _one(sh, bang, "; ".join(parts) + "; true", runner)
    out = {}
    for line in r.out.splitlines():
        k, _, v = line.partition(" ")
        if v.strip().isdigit():
            out[k] = int(v)
    return out


def stale(ages: dict, lane: str) -> list:
    """Readers started before the manifest they read was last written (loaded once at start)."""
    def ref(name):
        return ages.get("post_manifest") if name == "flax-post-biosd" else ages.get("manifest")
    return [n for n in _READERS if n in ages and ref(n) is not None and ages[n] < ref(n)]


def agent_stop_cmds(comp, agent: str) -> list:
    return [on_cmd(comp, [agent]),
            "sudo -n bash -c " + shlex.quote(compose_argv(comp.compose, "stop", agent))]


def agent_start_cmd(comp, agent: str) -> str:
    return [c for c in _recreate(comp, agent) if "--force-recreate" in c][0]


def api_image(sh, bang: str, runner) -> Optional[str]:
    r = _one(sh, bang, f"docker inspect {API} --format '{{{{.Config.Image}}}}'", runner)
    return (r.out.strip() or None) if r.rc == 0 else None


def post_bios_targets(sh, bang: str, runner) -> dict:
    """{family: target} of post's rendered BIOS manifest on `bang`."""
    r = _one(sh, bang, "cat /etc/flax/bios-firmware-versions.json", runner)
    try:
        return {e.get("family"): e.get("target") for e in json.loads(r.out)} if r.rc == 0 else {}
    except (ValueError, AttributeError, TypeError):
        return {}


def clean_rows(sh, bang: str, spec: dict, runner, image: Optional[str] = None) -> dict:
    """Pipe fwreset_remote.py into the running api container, or (image=, on a standby
    where triage is not running) into a one-shot container of the holder's api image."""
    stdin = f"SPEC = {spec!r}\n{_SCRIPT.read_text()}\nmain(SPEC)\n"
    cmd = (f"docker exec -i {API} python -" if image is None else
           f"docker run --rm -i --pull=never -v /etc/flax:/etc/flax -v /run/flax:/run/flax:ro "
           f"-w /home/app/api {shlex.quote(image)} python -")
    r = _one(sh, bang, cmd, runner, stdin=stdin, timeout=CLEAN_TIMEOUT)
    if r.rc:
        raise Refused(f"{bang}: row clean failed: {(r.err or r.out).strip()[-300:]}")
    try:
        return json.loads(r.out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Refused(f"{bang}: row clean failed: unreadable output {r.out.strip()[-200:]!r}")
