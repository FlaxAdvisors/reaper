"""Bang-side probe: collect one bang's facts and print them as JSON.

Standalone on purpose -- reaper pipes this file into `python3.12 -` on the bang,
prefixed by a `SPEC = {...}` line and followed by `main(SPEC)`. Stdlib only, never
imports reapercli. Read-only: it never reads env files, credentials or vaults.
Each collector catches its own failure, so a dead docker or psql never aborts it.
"""
import glob
import json
import os
import re
import subprocess

PSQL = ["docker", "exec", "flax-stack-postgres-1", "psql", "-U", "postgres", "-d", "flax", "-tAc"]


def sh(cmd: list, timeout: float = 8) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr.strip().splitlines() or [f"rc={p.returncode}"])[-1][:160])
    return p.stdout


def guarded(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 -- a probe reports, it never dies
        return {"error": f"{type(e).__name__}: {e}"[:200]}


# ------------------------------------------------------------------ pure parsers
def parse_df(text: str) -> list:
    """One row per filesystem (btrfs subvolumes share a device): the shortest mount wins."""
    best = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 6 and f[4].endswith("%"):
            if f[0] not in best or len(f[5]) < len(best[f[0]]["mount"]):
                best[f[0]] = {"mount": f[5], "used_pct": int(f[4][:-1])}
    return list(best.values())


def parse_systemctl_show(text: str) -> dict:
    out = {}
    for block in text.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" not in kv:
            continue
        name = re.sub(r"\.(service|timer)$", "", kv["Id"])
        out[name] = {"active": kv.get("ActiveState", ""), "sub": kv.get("SubState", ""),
                     "enabled": kv.get("UnitFileState", ""), "load": kv.get("LoadState", "loaded"),
                     "restarts": int(kv.get("NRestarts") or 0),
                     "since": kv.get("ActiveEnterTimestamp", "")}
    return out


def parse_docker_ps(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line.strip():
            d = json.loads(line)
            out[d["Names"]] = {"state": d.get("State", ""), "status": d.get("Status", ""),
                               "image": d.get("Image", "")}
    return out


def parse_docker_inspect(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line.strip():
            d = json.loads(line)
            st = d.get("state") or {}
            out[d["name"].lstrip("/")] = {"health": (st.get("Health") or {}).get("Status", ""),
                                          "restarts": int(d.get("restarts") or 0),
                                          "started": st.get("StartedAt", ""),
                                          "pid": int(st.get("Pid") or 0),
                                          "binds": sorted([m.get("Source", ""), m.get("Destination", "")]
                                                          for m in d.get("mounts") or []
                                                          if m.get("Type") == "bind"
                                                          and _FLAX_JSON.match(m.get("Source", "")))}
    return out


_FLAX_JSON = re.compile(r"^/etc/flax/[^/]+\.json$")


def parse_bind_stat(text: str, conts: dict, host: dict) -> dict:
    """`stat -c '%i %n' /proc/<pid>/root<dst>` output -> {container: {src: [host_ino, seen_ino]}}."""
    seen = {}
    for line in text.splitlines():
        ino, _, path = line.partition(" ")
        if ino.isdigit():
            seen[path] = int(ino)
    out = {}
    for name, c in conts.items():
        for src, dst in c.get("binds", ()):
            out.setdefault(name, {})[src] = [host.get(src), seen.get(f"/proc/{c.get('pid')}/root{dst}")]
    return out


def bind_inodes(conts: dict) -> dict:
    """A rename (every template/copy write) gives the host path a new inode; a running
    container keeps the old one. One sudo stat reads what each container sees."""
    live = {n: c for n, c in conts.items() if isinstance(c, dict) and c.get("pid") and c.get("binds")}
    host = {}
    for src in {s for c in live.values() for s, _ in c["binds"]}:
        try:
            host[src] = os.stat(src).st_ino
        except OSError:
            pass
    paths = [f"/proc/{c['pid']}/root{dst}" for c in live.values() for _, dst in c["binds"]]
    if not paths:
        return {}
    p = subprocess.run(["sudo", "-n", "stat", "-L", "-c", "%i %n", *paths], capture_output=True, text=True, timeout=8)
    if not p.stdout and p.returncode:
        raise RuntimeError((p.stderr.strip().splitlines() or ["stat failed"])[-1])
    return parse_bind_stat(p.stdout, live, host)


def parse_timers(text: str) -> dict:
    def sec(us):
        return us / 1e6 if us else None
    return {t["unit"].removesuffix(".timer"): {"last": sec(t.get("last")), "next": sec(t.get("next"))}
            for t in json.loads(text or "[]")}


def parse_ss_listen(text: str) -> list:
    ports = set()
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and ":" in f[3]:
            port = f[3].rsplit(":", 1)[1]
            if port.isdigit():
                ports.add(int(port))
    return sorted(ports)


def parse_tailscale(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        f = line.split(None, 4)
        if len(f) < 5 or not re.match(r"^\d+\.\d+\.\d+\.\d+$", f[0]):
            continue
        rest = f[4]
        out[f[1]] = ("offline" if "offline" in rest else "relay" if "relay" in rest
                     else "direct" if "direct" in rest else "idle")
    return out


# ------------------------------------------------------------------ collectors
def host(spec: dict) -> dict:
    mem = dict(re.findall(r"^(\w+):\s+(\d+)", open("/proc/meminfo").read(), re.M))
    out = {"uptime_s": float(open("/proc/uptime").read().split()[0]),
           "load1": float(open("/proc/loadavg").read().split()[0]),
           "mem_avail_pct": round(100 * int(mem["MemAvailable"]) / int(mem["MemTotal"]), 1),
           "disks": guarded(lambda: parse_df(sh(["df", "-P", "-x", "iso9660", "-x", "squashfs", "-x", "tmpfs",
                                                 "-x", "devtmpfs", "-x", "overlay"]))),
           "failed_units": guarded(lambda: [l.split()[0] for l in sh(
               ["systemctl", "--failed", "--no-legend", "--plain"]).splitlines() if l.strip()]),
           "tailscale": guarded(lambda: parse_tailscale(sh(["tailscale", "status"]))),
           "addrs": guarded(lambda: re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", sh(["ip", "-o", "-4", "addr"])))}
    chrony = guarded(lambda: re.search(r"System time\s+:\s+([\d.]+)", sh(["chronyc", "tracking"])).group(1))
    out["chrony_offset_s"] = float(chrony) if isinstance(chrony, str) else chrony
    return out


def units(spec: dict) -> dict:
    names = [f"{u}.service" for u in spec["units"]]
    props = "Id,LoadState,ActiveState,SubState,UnitFileState,NRestarts,ActiveEnterTimestamp"
    got = parse_systemctl_show(sh(["systemctl", "show", "-p", props, *names]))
    present = sh(["systemctl", "list-units", "--all", "--no-legend", "--plain",
                  "flax-*", "kea-*", "prod-*", "health-*"])
    got["_present"] = sorted({l.split()[0] for l in present.splitlines() if l.strip()})
    return got


def containers(spec: dict) -> dict:
    ps = parse_docker_ps(sh(["docker", "ps", "-a", "--format", "{{json .}}"]))
    if ps:
        fmt = ('{"name":{{json .Name}},"state":{{json .State}},"restarts":{{.RestartCount}},'
               '"mounts":{{json .Mounts}}}')
        for name, d in parse_docker_inspect(sh(["docker", "inspect", "--format", fmt, *ps])).items():
            ps[name].update(d)
    return ps


def _stamp(path: str) -> str:
    text = sh(["tar", "-xzOf", path, "./BUILD"]) if path.endswith(".tgz") else open(path).read(200)
    return (text.strip().splitlines() or [""])[0]


def files(spec: dict) -> dict:
    out = {}
    for path in spec["files"]:   # one guard per file: an old tarball without BUILD hides nothing else
        exists = os.path.exists(path)
        stamp = guarded(_stamp, path) if exists else ""
        out[path] = {"exists": exists, "stamp": stamp if isinstance(stamp, str) else "",
                     **({"error": stamp["error"]} if isinstance(stamp, dict) else {})}
    return out


def keepalived(spec: dict) -> dict:
    state = subprocess.run(["systemctl", "is-active", "keepalived"], capture_output=True, text=True)
    events = guarded(lambda: sh(["sudo", "-n", "journalctl", "-t", "Keepalived_vrrp", "--since", "-24h",
                                 "-o", "short-iso", "--no-pager"]).splitlines())
    if isinstance(events, list):   # state changes only; keepalived also logs every docker veth
        events = [e for e in events if re.search(r"Entering|STATE|priority", e)][-20:]
    return {"state": state.stdout.strip(), "vrrp_events": events}


def pg(spec: dict) -> dict:
    def q(sql):
        return sh(PSQL + [sql]).strip()
    out = {"in_recovery": q("select pg_is_in_recovery()") == "t",
           "alembic": guarded(lambda: q("select version_num from alembic_version"))}
    if out["in_recovery"]:
        out["wal_receiver"] = q("select coalesce(max(status),'') from pg_stat_wal_receiver")
        out["replay_lag_bytes"] = int(q("select coalesce(pg_wal_lsn_diff(pg_last_wal_receive_lsn(),"
                                        " pg_last_wal_replay_lsn()),0)::bigint") or 0)
    else:
        rows = q("select client_addr, state, coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(),"
                 " replay_lsn),0)::bigint from pg_stat_replication")
        out["replication"] = [{"addr": a, "state": st, "lag_bytes": int(lag or 0)}
                              for a, st, lag in (r.split("|") for r in rows.splitlines() if r)]
        out["slot_retained_bytes"] = int(q("select coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(),"
                                           " restart_lsn)),0)::bigint from pg_replication_slots") or 0)
        out["freshness"] = {t: guarded(lambda t=t, c=c: int(float(q(
            f"select extract(epoch from now()-max({c})) from {t}") or -1)))
            for t, c in spec["freshness"].items()}
    return out


def deployed(spec: dict) -> dict:
    return {os.path.basename(f)[:-5]: json.load(open(f)) for f in glob.glob("/etc/flax/deployed/*.json")}


SHARED_VENV = "/opt/flax/venv/reaper-ansible"   # apply_reaper_toolchain; what the shim uses on a bang


def tool(name: str, venv: str) -> str:
    """The toolchain binary the shim would run: the venv's when there is one."""
    return f"{venv}/bin/{name}" if venv else name


def toolchain(spec: dict) -> dict:
    venv = SHARED_VENV if os.path.isfile(f"{SHARED_VENV}/bin/python") else ""

    def ver(cmd, pat):
        try:
            m = re.search(pat, sh(cmd))
        except Exception:  # noqa: BLE001 -- a missing tool reads as ""
            return ""
        return m.group(1) if m else ""
    out = {"venv": venv or "none (system python3.12 + distro ansible)",
           "ansible-core": ver([tool("ansible", venv), "--version"], r"core ([\d.]+)"),
           "python": ver([tool("python", venv) if venv else "python3.12", "-V"], r"Python ([\d.]+)"),
           "docker": ver(["docker", "version", "--format", "{{.Server.Version}}"], r"([\w.-]+)"),
           "compose": ver(["docker", "compose", "version", "--short"], r"([\d.]+)"),
           "buildx": ver(["docker", "buildx", "version"], r"\sv?(\d+\.\d+\.\d+)"),
           "rsync": ver(["rsync", "--version"], r"version\s+([\d.]+)"),
           "git": ver(["git", "--version"], r"version ([\d.]+)")}
    dists = ("PyYAML", "Jinja2", "ansible-pylibssh", "paramiko")
    if venv:                                        # the venv's packages, not this probe's python
        got = guarded(lambda: json.loads(sh([tool("python", venv), "-c",
              "import json, importlib.metadata as m\n"
              "def v(d):\n try: return m.version(d)\n except m.PackageNotFoundError: return ''\n"
              f"print(json.dumps({{d: v(d) for d in {dists!r}}}))"])))
        out.update(got if isinstance(got, dict) and "error" not in got else dict.fromkeys(dists, ""))
    else:
        import importlib.metadata as md             # the python running this probe
        for dist in dists:
            try:
                out[dist] = md.version(dist)
            except md.PackageNotFoundError:
                out[dist] = ""
    galaxy = ["env", f"ANSIBLE_COLLECTIONS_PATH={venv}/collections"] if venv else []
    listing = guarded(lambda: sh([*galaxy, tool("ansible-galaxy", venv), "collection", "list"], timeout=30))
    if isinstance(listing, str):
        out.update(dict(re.findall(r"^([a-z_]+\.[a-z_]+)\s+([\d.]+)\s*$", listing, re.M)))
    return out


def collect(spec: dict) -> dict:
    facts = {"host": guarded(host, spec)}
    addrs = facts["host"].get("addrs") if isinstance(facts["host"], dict) else None
    facts["vip"] = bool(spec.get("vip")) and isinstance(addrs, list) and spec["vip"] in addrs
    # One bounded docker check first: a HUNG daemon would otherwise cost 8 s per docker
    # call and blow the whole probe's ssh budget, losing every other fact.
    docker = guarded(lambda: sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=5))
    down = {"error": f"docker not answering: {docker['error']}"} if isinstance(docker, dict) else None
    for key, fn in (("units", units), ("containers", containers), ("files", files),
                    ("keepalived", keepalived), ("pg", pg), ("deployed", deployed)):
        facts[key] = down if down and key in ("containers", "pg") else guarded(fn, spec)
    conts = facts["containers"] if isinstance(facts["containers"], dict) and "error" not in facts["containers"] else {}
    facts["bind_inodes"] = guarded(bind_inodes, conts)
    facts["timers"] = guarded(lambda: parse_timers(sh(["systemctl", "list-timers", "--all", "--output=json"])))
    facts["crons"] = guarded(lambda: sorted(os.listdir("/etc/cron.d")))
    facts["listen"] = guarded(lambda: parse_ss_listen(sh(["ss", "-ltnH"])))
    facts["images"] = down or guarded(lambda: sorted(sh(["docker", "images", "--format",
                                                         "{{.Repository}}:{{.Tag}}"]).split()))
    if spec.get("toolchain"):
        facts["toolchain"] = guarded(toolchain, spec)
    return facts


def main(spec: dict) -> None:
    print(json.dumps(collect(spec)))
