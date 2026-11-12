"""Rows: the one shape every command reports in (spec §4 output, §7)."""
import json
from dataclasses import asdict, dataclass
from typing import Iterable

# worst first. "unreachable": reaper could not ask the host, so it does not know
# (not the same as a FAIL it saw); it still exits 1, the state is unconfirmed.
STATUSES = ("FAIL", "unreachable", "warn", "ok", "info", "dormant")
UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Row:
    section: str
    item: str
    status: str
    value: str = ""
    host: str = ""
    site: str = ""


def worst(statuses: Iterable[str]) -> str:
    seen = set(statuses)
    for s in STATUSES:
        if s in seen:
            return s
    return "ok"


def exit_code(rows: list) -> int:
    return 1 if any(r.status in ("FAIL", UNREACHABLE) for r in rows) else 0


def render(rows: list, as_json: bool = False) -> str:
    if as_json:
        return json.dumps([asdict(r) for r in rows], indent=1)
    lines = []
    for r in rows:
        where = "/".join(x for x in (r.site, r.host) if x)
        lines.append(f"{where:<24} {r.section:<12} {r.item:<28} {r.status:<11} {r.value}".rstrip())
    return "\n".join(lines)
