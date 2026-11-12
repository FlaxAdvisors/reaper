"""Stale single-file bind mounts (memory single-file-bind-mounts-go-stale).

A role's template/copy write is a rename: the host path gets a NEW inode, while a
running container keeps the inode it mounted and reads the old content forever. The
probe stats each mounted /etc/flax/*.json on the host and through /proc/<pid>/root
(what the container sees); stale = the two inodes differ. Mounts come from docker
inspect (the truth on the bang), not the registry. ctime is NOT used: in-place writes
bump it without changing what the container reads. Stopped containers don't count:
a vip-gated service sitting on the standby is not stale."""
from reapercli.lib.out import Row


def stale_binds(facts: dict) -> list:
    if "error" in facts or facts.get("dormant"):
        return []
    conts, inodes = facts.get("containers"), facts.get("bind_inodes")
    if not isinstance(conts, dict) or not isinstance(inodes, dict) or "error" in inodes:
        return []
    out = []
    for name, paths in inodes.items():
        if (conts.get(name) or {}).get("state") != "running":
            continue
        for path, (host, seen) in paths.items():
            if host is not None and seen is not None and host != seen:
                out.append((name, path))
    return sorted(out)


def stale_rows(bang: str, facts: dict) -> list:
    return [Row("stale-bind", name, "warn", f"{path.rsplit('/', 1)[-1]} replaced on host since start", bang)
            for name, path in stale_binds(facts)]


def restart_hint(site: str, bang: str, container: str) -> str:
    return f"reaper svc restart {site} {container} --host {bang} --yes"
