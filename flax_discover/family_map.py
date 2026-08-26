"""Product Name → family classification.

Lifted VERBATIM from ``scripts/reaper_leased.py`` (section 2. family_map).
These functions MUST stay byte-identical to the legacy enroller's
classification logic so the new pipeline classifies devices identically
to the legacy daemon.
"""

import re
import glob
import os


def compile_family_map_text(text):
    """Compile one family-map .txt's lines into a list of regex objects.

    Per spec: leading whitespace is stripped (input files have a leading
    space per line; treat as accidental). Empty lines and comment lines
    (starting with '#' after lstrip) are skipped.
    """
    out = []
    for raw in text.splitlines():
        line = raw.lstrip()
        if not line or line.startswith("#"):
            continue
        out.append(re.compile(line))
    return out


def load_family_map_dir(path):
    """Load all <family>.txt files under path → dict[family_name, [regex]]."""
    fm = {}
    for fpath in sorted(glob.glob(os.path.join(path, "*.txt"))):
        family = os.path.splitext(os.path.basename(fpath))[0]
        with open(fpath) as f:
            patterns = compile_family_map_text(f.read())
        if patterns:
            fm[family] = patterns
    return fm


def match_family(fm, product_name):
    """Walk every family's regex list. First match wins. Returns family or None."""
    if not product_name:
        return None
    for family, patterns in fm.items():
        for pat in patterns:
            if pat.search(product_name):
                return family
    return None
