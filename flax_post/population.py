# flax_post/population.py
"""Ghost node_config string-match population profiles (docs/Post-UI-Design.md §5.6).

A profile is a file of rules, one per line, each a FULL-LINE PRESENCE REGEX
matched (re.search) against the macinv COUNT-form output. macinv's count form
collapses identical components into one `uniq -c`-prefixed line, e.g.
`8 Memory Size: 32 GB, ...` or `2 Processor Version: ...Gold 6138...`. A profile
rule like `8 Memory Size: 32 GB` or `2 Processor.*Gold 6138.*2.00GHz` is written
to match exactly that line -- the leading integer is PART OF THE REGEX (it
matches the uniq -c count prefix), NOT a separate expected line-count. So a
12x64GB node's line `12 Memory Size: 64 GB` correctly fails an `8 ...32 GB` rule
(different count AND size), and passes a `12 ...64 GB` rule. Rules without a
leading integer are ordinary presence assertions (BIOS/product strings).

evaluate() reports per-rule presence (found/ok) + a blade-level ok for the POP
verdict. Catalog is the ghost-published dir /opt/flax/node_config (read
directly; no new path).
"""
import os
import re

PROFILE_DIR = os.environ.get("FLAX_POST_PROFILE_DIR", "/opt/flax/node_config")


def list_profiles() -> list:
    """Profile filenames (sorted); skip hidden + editor-backup (~) files."""
    try:
        names = os.listdir(PROFILE_DIR)
    except OSError:
        return []
    return sorted(n for n in names
                  if not n.startswith(".") and not n.endswith("~")
                  and os.path.isfile(os.path.join(PROFILE_DIR, n)))


def parse_profile(text: str) -> list:
    """Lines -> [rule_str]. Skips blank/`#`-comment lines; strips surrounding
    single quotes. The WHOLE remaining line (leading uniq -c integer included)
    is the presence regex -- no integer is split out."""
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) >= 2 and line[0] == "'" and line[-1] == "'":
            line = line[1:-1]
        rules.append(line)
    return rules


def load_profile(name: str) -> list:
    """Parse the named profile file (empty list if absent/unreadable)."""
    try:
        with open(os.path.join(PROFILE_DIR, name)) as f:
            return parse_profile(f.read())
    except OSError:
        return []


def evaluate(rules: list, dump: str) -> dict:
    """Run each full-line presence-regex rule against the count-form `dump`;
    per-rule found/ok + blade-level ok. A rule passes if at least one dump line
    matches it (the leading count integer, if any, rides inside the regex)."""
    lines = dump.splitlines()
    results = []
    for rule in rules:
        try:
            rx = re.compile(rule)
        except re.error:
            results.append({"rule": rule, "found": 0, "ok": False})
            continue
        found = sum(1 for ln in lines if rx.search(ln))
        results.append({"rule": rule, "found": found, "ok": found >= 1})
    return {"ok": all(r["ok"] for r in results), "results": results}
