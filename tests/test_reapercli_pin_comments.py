"""Image-tag pins carry a one-line comment; history lives in git log
(spec 2026-11-07 §11 Phase 0)."""
import os
import pathlib
import re

REAPER = pathlib.Path(__file__).resolve().parents[1]
PIN = re.compile(r"^\s*\w+_image_tag:")


def test_image_tag_lines_are_short():
    long_lines = []
    seen = 0
    for site in ("eindhoven", "lavergne", "braintree"):
        f = REAPER / "inventories" / site / "group_vars" / "all" / "vars.yml"
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if PIN.match(line):
                seen += 1
                if len(line) > 160:
                    long_lines.append(f"{site}:{n} ({len(line)} chars)")
    assert seen > 10, "found almost no pins (presence proof)"
    assert long_lines == []
