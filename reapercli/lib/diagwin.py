"""reaper diag's time window (spec 2026-11-08-reaper-diag-design): 12 h by default, never
more than 72 h back. Times are read and shown in the local zone of the machine running
reaper; every bang is UTC, so the conversion is exact."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from reapercli.lib.sites import ScopeError

DEFAULT = timedelta(hours=12)
LIMIT = timedelta(hours=72)
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
FORMS = "30m, 2h, 3d, 14:00, yesterday 14:00, 2026-11-08 14:00"


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    clamped: bool


def parse_length(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", text or "")
    if not m:
        raise ScopeError(f"bad length {text!r}: use 30m, 2h or 3d")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def parse_since(text: str, now: datetime, local: bool = False) -> datetime:
    """local=True: a clock time is in the system zone ON THAT DATE (DST-aware), not
    now's fixed offset. Tests pass a fixed zone in `now` and leave local False."""
    t = (text or "").strip()
    if re.fullmatch(r"\d+\s*[mhd]", t):
        return now - parse_length(t)
    m = (re.fullmatch(r"(?:(yesterday)\s+)?(\d{1,2}):(\d{2})", t)
         or re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})", t))
    if not m:
        raise ScopeError(f"bad --since {text!r}: use one of {FORMS}")
    day, hh, mm = m.groups()
    try:
        if day and day != "yesterday":
            base = datetime.strptime(day, "%Y-%m-%d").date()
        else:
            base = (now - timedelta(days=1) if day == "yesterday" else now).date()
        naive = datetime(base.year, base.month, base.day, int(hh), int(mm))
    except ValueError:
        raise ScopeError(f"bad --since {text!r}: no such date or time") from None
    start = naive.astimezone() if local else naive.replace(tzinfo=now.tzinfo)
    if start > now:
        raise ScopeError(f"--since {text!r} starts in the future")
    return start


def window(since: Optional[str], length: Optional[str], now: datetime, local: bool = False) -> Window:
    asked = parse_since(since, now, local) if since else now - DEFAULT
    end = min(asked + parse_length(length), now) if length else now
    if end <= now - LIMIT:
        raise ScopeError(f"that window ends more than 72 h ago: diag looks back 72 h at most")
    start = max(asked, now - LIMIT)
    return Window(start, end, asked < start)
