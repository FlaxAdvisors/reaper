"""Scan a captured SOL console (sol.txt) for known error patterns.

Each matching console line is one finding row; any finding fails the scan rung
(`install-console`), whose raw SOL stays an artifact either way. ANSI escapes and
carriage returns (the installer's curses UI, getty redraws) are stripped before
matching, so a pattern never splits on a colour code. The list grows over time:
add a (name, regex) pair to PATTERNS, never loosen a match to silence a real hit."""
import re
from pathlib import Path

from .result import UNMEASURED, RungResult

PATTERNS = (
    ("kernel panic", r"Kernel panic"),
    ("call trace", r"Call Trace"),
    ("subiquity error", r"subiquity.*(error|Traceback)"),
    ("curtin error", r"curtin.*(ERROR|failed)"),
    ("out of memory", r"Out of memory"),
    # not virtual media (the BMC's sr0) or a loop device: those print I/O errors on any boot
    ("I/O error", r"I/O error(?!.*\bdev (?:sr|loop)\d)"),
    ("ext4 error", r"EXT4-fs error"),
    ("machine check", r"mce: \[Hardware Error\]"),    # not mce: info lines (TM1 enabled)
)
_COMPILED = tuple((n, re.compile(rx)) for n, rx in PATTERNS)
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
MAX_ROWS = 50          # finding rows shown; the count covers them all
_LINE_MAX = 240


def clean(text: str) -> str:
    """Console text without ANSI escapes; a CR-overwritten line keeps its last write."""
    out = []
    for ln in _ANSI.sub("", text).split("\n"):
        ln = ln.rstrip("\r")
        out.append(ln.rsplit("\r", 1)[-1] if "\r" in ln else ln)
    return "\n".join(out)


def scan(text: str) -> list[dict]:
    """[{"pattern", "line" (1-based), "text"}] for every console line that matches; a
    line counts once, under the first pattern it matches."""
    hits = []
    for i, ln in enumerate(clean(text).split("\n"), 1):
        for name, rx in _COMPILED:
            if rx.search(ln):
                hits.append({"pattern": name, "line": i, "text": ln.strip()[:_LINE_MAX]})
                break
    return hits


def tail(path, n: int = 2000) -> str:
    """The last n characters of a (cleaned) console capture; '' when unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n * 4))
            raw = f.read()
    except OSError:
        return ""
    return clean(raw.decode("utf-8", "replace"))[-n:]


def scan_file(path) -> RungResult:
    """The console scan rung: pass when the capture holds bytes and no pattern hit; fail
    with one row per hit; unmeasured when there is no capture to scan."""
    p = Path(path)
    try:
        text = p.read_bytes().decode("utf-8", "replace")
    except OSError as e:
        why = f"no SOL capture to scan ({e.strerror or e})"
        return RungResult(UNMEASURED, {"headline": why, "reason": why, "error": why,
                                       "rows": [], "notes": []})
    if not text:
        why = "no SOL capture to scan (0 bytes)"
        return RungResult(UNMEASURED, {"headline": why, "reason": why, "error": why,
                                       "rows": [], "notes": []})
    hits = scan(text)
    counts: dict[str, int] = {}
    for h in hits:
        counts[h["pattern"]] = counts.get(h["pattern"], 0) + 1
    rows = [[f"line {h['line']}", f"{h['pattern']}: {h['text']}"] for h in hits[:MAX_ROWS]]
    notes = [f"patterns: {', '.join(rx for _, rx in PATTERNS)}"]
    if len(hits) > MAX_ROWS:
        notes.append(f"{len(hits) - MAX_ROWS} more finding(s) not shown; see the raw SOL")
    detail = {"findings": len(hits), "counts": counts, "bytes": len(text.encode()),
              "rows": rows, "notes": notes}
    if hits:
        head = f"{len(hits)} error line(s) in SOL: " + ", ".join(
            f"{k} x{v}" for k, v in counts.items())
        return RungResult("fail", {**detail, "headline": head, "error": head})
    return RungResult("pass", {**detail, "headline": "no error patterns in SOL"})
