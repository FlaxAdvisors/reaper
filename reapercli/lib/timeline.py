"""reaper diag: sources -> events (facts with a time and a source, never opinions),
folded so a service failing every 10 s is one line, plus how far back each source
actually reached. Times are aware UTC here; diagout renders them in the local zone."""
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from reapercli.lib.diagsrc import (UTC, _first_journal, _first_stamped, _lines, _log_start,  # noqa: F401
                                   _raw_events, gmt, iso, journal, stamped)


@dataclass(frozen=True)
class Event:
    t: datetime
    bang: str
    source: str
    text: str
    count: int = 1
    last: Optional[datetime] = None


def events(bang: str, sources: dict, win) -> list:
    lo, hi = win.start.astimezone(UTC), win.end.astimezone(UTC)
    out, seen = [], set()
    for name, s in sorted(sources.items()):
        for t, label, text in _raw_events(name, s.get("out", ""), sources):
            # journalctl -u gives systemd's line about a unit and the unit's own copy of it
            dup = (t.replace(microsecond=0), label, text)
            if lo <= t <= hi and dup not in seen:
                seen.add(dup)
                out.append(Event(t, bang, label, text))
    return sorted(out, key=lambda e: e.t)


NOISE = 10   # a line seen this often in the window, on one bang+source, is noise, not an event


def fold(evs: list) -> list:
    """Lines (digits masked) that recur NOISE+ times on one bang+source fold into one line
    with count and last time: a timer's 700 'Deactivated', smartctl's 4-line error cycle.
    Rarer lines stay individual events, so separate keepalived cycles keep their times."""
    def key(e):   # docker lines name a container and an exit code: digits there are identity
        return e.bang, e.source, e.text if e.source == "docker" else re.sub(r"\d+", "#", e.text)
    evs = sorted(evs, key=lambda e: e.t)
    seen = {}
    for e in evs:
        seen.setdefault(key(e), []).append(e)
    out = []
    for group in seen.values():
        if len(group) >= NOISE:
            out.append(replace(group[0], count=len(group), last=group[-1].t))
        else:
            out += group
    return sorted(out, key=lambda e: e.t)


def reach(bang: str, sources: dict, win) -> list:
    """(label, reaches back to, truncated) for each source that does not cover the
    whole window: journals since the oldest boot, a container's logs since it was
    created or since its oldest kept (rotated) log file, docker events since the
    daemon started. A source cut to its newest 5 MB reaches back to its oldest kept line."""
    lo, got = win.start.astimezone(UTC), []

    def add(label, since, cut):
        if cut or (since and since > lo):
            got.append((label, since, cut))
    boots = [b for line in sources.get("host.boots", {}).get("out", "").splitlines()[1:] for b in gmt(line)[:1]]
    journals = [n for n in sources if n.endswith(".journal")]
    if journals:
        add("journal", min(boots) if boots else None, False)
        for n in journals:
            if sources[n].get("truncated"):
                add(n[:-8], _first_journal(sources[n].get("out", "")), True)
    for n, s in sources.items():
        if n.startswith("container.") and n.endswith(".logs") and not s.get("absent"):
            label = n[10:-5]
            try:
                created = iso(json.loads(sources.get(f"container.{label}.inspect", {}).get("out") or "[{}]")[0].get("Created", ""))
            except (ValueError, IndexError, AttributeError):
                created = None
            starts = [t for t in (created, _log_start(sources.get(f"container.{label}.logstart", {}).get("out", "")),
                                  _first_stamped(s.get("out", "")) if s.get("truncated") else None) if t]
            add(label, max(starts) if starts else None, bool(s.get("truncated")))
    if "pg.log" in sources and sources["pg.log"].get("truncated"):
        add("postgres", _first_stamped(sources["pg.log"].get("out", "")), True)
    if "docker.events" in sources:
        first = next((datetime.fromtimestamp(int(o.get("time") or 0), UTC)
                      for o in _lines(sources["docker.events"].get("out", ""))), None)
        got.append(("docker events (the daemon keeps its last 256 only; lifecycle also from inspect)",
                    first, bool(sources["docker.events"].get("truncated"))))
    return got
