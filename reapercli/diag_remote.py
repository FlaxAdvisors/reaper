"""Bang-side collector for `reaper diag`: run a fixed set of READ-ONLY commands and
print {"now": epoch, "sources": {name: {cmd, rc, out, err, truncated}}} as JSON.

Standalone on purpose (like probe_remote.py): reaper pipes this file into
`python3.12 -` on the bang, prefixed by `SPEC = {...}` and followed by `main(SPEC)`.
Stdlib only, never imports reapercli. Every source is guarded: one failure never
stops the rest. Nothing here writes: no restart, no compose up, no DML, no docker exec
except psql SELECTs and kea's status-get.
"""
import json
import re
import shlex
import subprocess
import time

PSQL = "docker exec flax-stack-postgres-1 psql -U postgres -d flax -tAc "
FIELDS = "PRIORITY,SYSLOG_IDENTIFIER,MESSAGE,_SYSTEMD_UNIT"
# filtered ON the bang, so the 5 MB cap holds evidence, not noise (measured live 2026-11-08):
KERNEL = "(?i)out of memory|oom|killed process|i/o error|link is (up|down)|segfault|call trace|hung task"
EVENTS = ("create", "start", "die", "oom", "kill", "stop", "destroy", "restart")   # never exec_*
PG_NOISE = "connection received|connection authorized|disconnection: session time"
KEA_STATUS = ("import json,socket,sys\ns=socket.socket(socket.AF_UNIX);s.settimeout(5);s.connect(sys.argv[1])\n"
              "s.sendall(json.dumps({'command':'status-get'}).encode());b=b''\n"
              "while True:\n c=s.recv(65536)\n if not c: break\n b+=c\nprint(b.decode())")


def run(cmd: str, timeout: float = 60):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def cap_tail(text: str, cap: int):
    """The newest `cap` BYTES of text (UTF-8), cut on a character boundary."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= cap:
        return text, False
    return raw[-cap:].decode("utf-8", "ignore"), True


def source(cmd: str, cap: int, timeout: float = 60, drop: str = "", quiet_rc1: bool = False) -> dict:
    """drop: a regex of noise lines removed BEFORE the cap. quiet_rc1: rc 1 with nothing on
    stderr means "no match" (journalctl --grep), not a failure; any message keeps it failed."""
    try:
        rc, out, err = run(cmd, timeout)
    except Exception as e:  # noqa: BLE001 -- a collector reports, it never dies
        rc, out, err = 1, "", f"{type(e).__name__}: {e}"
    if drop:
        out = "".join(l for l in out.splitlines(keepends=True) if not re.search(drop, l))
    if quiet_rc1 and rc == 1 and not err.strip():
        rc = 0
    out, cut = cap_tail(out, cap)
    return {"cmd": cmd, "rc": rc, "out": out, "err": err[-2000:], "truncated": cut}


def journal(selector: str, spec: dict) -> str:
    return (f"sudo -n journalctl {selector} --since @{spec['since']} --until @{spec['until']} "
            f"-o json --all --output-fields={FIELDS} --no-pager")


def docker_logs(name: str, spec: dict) -> str:
    return f"docker logs --timestamps --since {spec['since']} --until {spec['until']} {shlex.quote(name)} 2>&1"


def pg_facts() -> str:
    """One source: a JSON object of read-only facts, built on the bang."""
    def q(sql):
        rc, out, err = run(PSQL + shlex.quote(sql), 20)
        return out.strip() if rc == 0 else f"ERROR: {err.strip()[-200:]}"
    f = {"in_recovery": q("select pg_is_in_recovery()"),
         "alembic": q("select version_num from alembic_version"),
         "replication": q("select client_addr, state, coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(),"
                          " replay_lsn),0)::bigint from pg_stat_replication"),
         "slots": q("select slot_name, active, coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(),"
                    " restart_lsn),0)::bigint from pg_replication_slots"),
         "wal_receiver": q("select coalesce(max(status),'') from pg_stat_wal_receiver"),
         "replay_lag_bytes": q("select coalesce(pg_wal_lsn_diff(pg_last_wal_receive_lsn(),"
                               " pg_last_wal_replay_lsn()),0)::bigint"),
         "standby_signal": run("sudo -n test -f /opt/flax/var/postgres/standby.signal", 10)[0] == 0}
    return json.dumps(f)


def gather(spec: dict) -> dict:
    cap, s, t0 = spec["cap"], {}, time.monotonic()

    def sx(name, cmd, timeout=60, **kw):
        # one global deadline, under reaper's ssh timeout: a hung source costs its own
        # time, never the whole bang's bundle
        left = spec.get("deadline", 150) - (time.monotonic() - t0)
        if left <= 1:
            s[name] = {"cmd": cmd, "rc": 124, "out": "", "err": "skipped: deadline", "truncated": False}
            return
        s[name] = source(cmd, cap, min(timeout, left), **kw)
    sx("host.uptime", "uptime")
    sx("host.boots", "sudo -n journalctl --list-boots --no-pager -o short-iso")
    # the oldest listed boot's FIRST ENTRY is a boot only if the journal still holds that
    # boot's kernel start; after rotation it is just the oldest line kept
    rows = [l.split() for l in s["host.boots"]["out"].splitlines()[1:] if l.split()]
    if rows and re.fullmatch(r"-?\d+", rows[0][0]):
        sx("host.oldestboot", f"sudo -n journalctl -b {rows[0][0]} -k -g '^Linux version' -n 1 -q "
                              "--no-pager -o short-iso", quiet_rc1=True)
    sx("host.df", "df -P -x iso9660 -x squashfs -x tmpfs -x devtmpfs -x overlay")
    sx("host.free", "free -m")
    sx("host.failed", "systemctl --failed --no-legend --plain")
    sx("host.ipaddr", "ip -o -4 addr")
    sx("host.ss", "ss -ltnH")
    # _TRANSPORT=kernel, not -k (which means this boot only): an OOM before a reboot counts
    sx("kernel.journal", journal(f"_TRANSPORT=kernel --grep {shlex.quote(KERNEL)}", spec), 120, quiet_rc1=True)
    sx("keepalived.active", "systemctl is-active keepalived")
    sx("keepalived.journal", journal("-u keepalived", spec), 120)
    docker_up = run("docker info --format '{{.ServerVersion}}'", 5)[0] == 0
    sx("docker.daemon", "systemctl show docker -p ActiveEnterTimestamp --value")
    if docker_up:
        sx("docker.ps", "docker ps -a --format '{{json .}}'")
        sx("docker.events", f"docker events --since {spec['since']} --until {spec['until']} --filter type=container "
                            + "".join(f"--filter event={e} " for e in EVENTS) + "--format '{{json .}}'", 120)
        sx("pg.log", docker_logs("flax-stack-postgres-1", spec), 120, drop=PG_NOISE)
    else:
        for name in ("docker.ps", "docker.events", "pg.log"):
            s[name] = {"cmd": "", "rc": 124, "out": "", "err": "docker not answering", "truncated": False}
    sx("deployed", "for f in /etc/flax/deployed/*.json; do cat \"$f\"; echo; done")
    s["pg.facts"] = {"cmd": "psql SELECTs", "rc": 0, "out": pg_facts() if docker_up else "",
                     "err": "" if docker_up else "docker not answering", "truncated": False}
    for u in spec["units"]:
        sx(f"unit.{u}.status", f"systemctl status {shlex.quote(u)}.service --no-pager -l")
        sx(f"unit.{u}.journal", journal(f"-u {shlex.quote(u)}.service", spec), 120)
    for t in spec["timers"]:
        sx(f"timer.{t}.status", f"systemctl status {shlex.quote(t)}.timer --no-pager")
    present = set(run("docker ps -a --format '{{.Names}}'", 15)[1].split()) if docker_up else set()
    for c in spec["containers"]:
        if c == "flax-stack-postgres-1":
            continue                                  # collected once, filtered, as pg.log
        if c not in present:
            for kind in ("inspect", "logs"):
                s[f"container.{c}.{kind}"] = {"cmd": "", "rc": 0, "out": "", "err": "", "truncated": False,
                                              "absent": True}
            continue
        sx(f"container.{c}.inspect", f"docker inspect {shlex.quote(c)}")
        sx(f"container.{c}.logs", docker_logs(c, spec), 120)
        # json-file rotates (10m x 5 on the bangs): the oldest kept file's first line is how
        # far back this container's logs reach, whatever its creation time says
        sx(f"container.{c}.logstart",
           f"p=$(docker inspect -f '{{{{.LogPath}}}}' {shlex.quote(c)}) && "
           "f=$(sudo -n sh -c 'ls -1 \"$1\"*' _ \"$p\" | sort -V | tail -1) && sudo -n head -n 1 \"$f\"", 20)
    for comp in spec["compose"]:
        sx(f"compose.{comp['name']}.file", f"sudo -n cat {shlex.quote(comp['dir'] + '/' + comp['file'])}")
        if comp.get("env_file"):
            sx(f"compose.{comp['name']}.env", f"sudo -n cat {shlex.quote(comp['dir'] + '/' + comp['env_file'])}")
    if spec["kea"]:
        for v, cont in (("4", "kea-dhcp4"), ("6", "kea-dhcp6")):
            if cont not in present:
                s[f"kea.status{v}"] = {"cmd": "", "rc": 0, "out": "", "err": "", "truncated": False, "absent": True}
                continue
            sx(f"kea.status{v}", f"docker exec {cont} python3 -c {shlex.quote(KEA_STATUS)} "
                                 f"/var/run/kea/kea{v}-ctrl-socket", 20)
    if spec["drain"]:
        sx("triage.drain", "ls -l --time-style=+%Y-%m-%dT%H:%M:%SZ /run/flax/fw-drain/ 2>&1")
    if spec["extras"] and f"inet {spec['vip']}/" in s["host.ipaddr"]["out"]:
        sx("site.extras", spec["extras_script"], 60)
    return {"now": int(time.time()), "sources": s}


def main(spec: dict) -> None:
    print(json.dumps(gather(spec)))
