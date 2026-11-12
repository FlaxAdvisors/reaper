"""reaper diag sources -> raw (time, source, text) facts: the per-source parsers
(journald JSON, docker events/inspect/logs, postgres log, deploy records, boots)
and the reach helpers. timeline.py turns these into events, folds and reach."""

import json
import re
from datetime import datetime, timezone
from typing import Optional

UTC = timezone.utc
KEEPALIVED = re.compile(r"Entering|STATE|priority|notify|Script.*(failed|succeeded)")
KERNEL = re.compile(r"out of memory|oom|killed process|i/o error|link is (up|down)|segfault|call trace|hung task", re.I)
LIFECYCLE = re.compile(r"Started|Stopped|Stopping|Failed|failed|Main process exited|Scheduled restart|Deactivated")
LEVELS = re.compile(r"ERROR|WARNING|Traceback|FATAL|CRITICAL|PANIC")
PG = re.compile(r"promote|started streaming|replication terminated|ready to accept")
ACTIONS = {"create", "start", "die", "oom", "restart", "kill", "stop", "destroy"}
_GMT = re.compile(r"\w{3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?:GMT|UTC)")


def iso(text: str) -> Optional[datetime]:
    """docker's RFC 3339 with nanoseconds, or a plain ...Z stamp."""
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z", text or "")
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(microsecond=int(frac), tzinfo=UTC)


def gmt(text: str) -> list:
    return [datetime.strptime(m, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC) for m in _GMT.findall(text or "")]


def _one(v):
    """journald JSON: a repeated field is a list; a non-UTF-8 value is a list of byte values."""
    if isinstance(v, list):
        if v and all(isinstance(x, int) for x in v):
            return bytes(v).decode("utf-8", "replace")
        return _one(v[0]) if v else ""
    return "" if v is None else str(v)


def journal(text: str):
    for line in (text or "").splitlines():
        try:
            j = json.loads(line)
            t = datetime.fromtimestamp(int(_one(j["__REALTIME_TIMESTAMP"])) / 1e6, UTC)
            prio = int(_one(j.get("PRIORITY")) or 6)
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
        yield t, prio, _one(j.get("SYSLOG_IDENTIFIER")), _one(j.get("MESSAGE"))


def stamped(text: str):
    for line in (text or "").splitlines():
        stamp, _, rest = line.partition(" ")
        t = iso(stamp)
        if t:
            yield t, rest


def _records(text: str):
    """Concatenated JSON objects (the deploy records, pretty-printed). A broken one is
    skipped to the next object that starts a line, never ending the parse."""
    dec, i, text = json.JSONDecoder(), 0, text or ""
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError:
            j = text.find("\n{", i + 1)
            if j < 0:
                return
            i = j + 1
            continue
        yield obj


def _lines(text: str):
    """One JSON object per line (docker --format '{{json .}}'); a cut or bad line is skipped."""
    for line in (text or "").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _inspect_events(out: str):
    """Container lifecycle from docker inspect: the daemon's event buffer holds only its
    last 256 events (healthcheck execs fill it in minutes), inspect keeps the last start/exit."""
    try:
        items = json.loads(out or "[]")
    except ValueError:
        return
    for c in items if isinstance(items, list) else []:
        st, name = c.get("State") or {}, str(c.get("Name", "?")).lstrip("/")
        started, finished = iso(str(st.get("StartedAt", ""))), iso(str(st.get("FinishedAt", "")))
        if started and started.year > 1:
            yield started, "docker", f"{name} start"
        if finished and finished.year > 1 and not st.get("Running") and (not started or finished >= started):
            yield finished, "docker", f"{name} die exit={st.get('ExitCode')}{' oom' if st.get('OOMKilled') else ''}"


def _boots(sources: dict):
    """Boot times from --list-boots. The OLDEST listed boot counts only when the journal
    still holds its kernel start (host.oldestboot found `Linux version`): after
    rotation its FIRST ENTRY is merely the oldest line kept, not a boot."""
    rows = [gmt(l)[:1] for l in sources.get("host.boots", {}).get("out", "").splitlines()[1:]]
    rows = [r[0] for r in rows if r]
    probe = sources.get("host.oldestboot")
    if rows and probe is not None and not (probe.get("out") or "").strip():
        rows = rows[1:]
    return rows


def _raw_events(name: str, out: str, sources: Optional[dict] = None):
    if name == "keepalived.journal":
        for t, prio, ident, msg in journal(out):
            if KEEPALIVED.search(msg) or (ident == "systemd" and LIFECYCLE.search(msg)):
                yield t, "keepalived", msg
    elif name == "kernel.journal":
        for t, prio, ident, msg in journal(out):
            if KERNEL.search(msg):
                yield t, "kernel", msg
    elif name.startswith("unit.") and name.endswith(".journal"):
        for t, prio, ident, msg in journal(out):
            if (ident == "systemd" and LIFECYCLE.search(msg)) or prio <= 4:
                yield t, name[5:-8], msg
    elif name.startswith("container.") and name.endswith(".inspect"):
        yield from _inspect_events(out)
    elif name == "docker.events":
        for obj in _lines(out):
            if obj.get("Action") in ACTIONS:
                attrs = (obj.get("Actor") or {}).get("Attributes") or {}
                t = datetime.fromtimestamp(int(obj.get("timeNano") or int(obj.get("time", 0)) * 10**9) / 1e9, UTC)
                code = f" exit={attrs['exitCode']}" if "exitCode" in attrs else ""
                yield t, "docker", f"{attrs.get('name', '?')} {obj['Action']}{code}"
    elif name.startswith("container.") and name.endswith(".logs") or name == "pg.log":
        label = "postgres" if name == "pg.log" else name[10:-5]
        for t, rest in stamped(out):
            if LEVELS.search(rest) or (name == "pg.log" and PG.search(rest)):
                yield t, label, rest.strip()
    elif name == "deployed":
        for r in _records(out):
            t = iso(str(r.get("at", "")))
            if t:
                yield t, "deploy", (f"deploy {r.get('component')} {str(r.get('commit', ''))[:7]} "
                                    f"by {r.get('user')} from {r.get('from')}")
    elif name == "host.boots":
        for t in _boots(dict(sources or {}, **{"host.boots": {"out": out}})):
            yield t, "boot", "boot"


def _first_journal(out: str):
    return next((t for t, _p, _i, _m in journal(out)), None)


def _first_stamped(out: str):
    return next((t for t, _r in stamped(out)), None)


def _log_start(out: str):
    """The first line of a container's oldest kept json-file log: {"time": "..."}."""
    for obj in _lines(out):
        return iso(str(obj.get("time", "")))
    return None
