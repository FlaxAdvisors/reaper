#!/usr/bin/env python3
"""Keep reaper's Include lines at the TOP of an ssh config (used by ./setup).

OpenSSH treats an Include that follows a `Host`/`Match` line as part of that block,
so appending at the end of ~/.ssh/config made the includes apply to the last host
only. This moves (or adds) each wanted Include above every Host/Match block, treats
`~/x` and `/home/<u>/x` as the same file, and drops duplicates. Idempotent; the
previous file is kept as <config>.reaper-bak when anything changes.

usage: ssh-includes.py <ssh-config> <include-path>...
"""
import os
import re
import shutil
import sys

MARKER = "# reaper includes -- kept above every Host/Match block by ~/git/reaper/setup"
INCLUDE = re.compile(r"^\s*Include\s+(\S+)\s*$", re.IGNORECASE)


def norm(path):
    return os.path.normpath(os.path.expanduser(path))


def rewrite(text, wanted):
    want = {norm(w) for w in wanted}
    out, lines, skip_blank = [], text.splitlines(), False
    for line in lines:
        m = INCLUDE.match(line)
        if line.strip() == MARKER or (m and norm(m.group(1)) in want):
            skip_blank = True
            continue
        if skip_blank and not line.strip():
            skip_blank = False
            continue
        skip_blank = False
        out.append(line)
    rest = "\n".join(out).lstrip("\n")
    head = "\n".join([MARKER] + ["Include " + w for w in wanted]) + "\n"
    return head + ("\n" + rest + ("\n" if rest and not rest.endswith("\n") else "") if rest else "")


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    cfg, wanted = argv[1], argv[2:]
    old = open(cfg).read() if os.path.exists(cfg) else ""
    new = rewrite(old, wanted)
    if new == old:
        print("  ok ssh includes already at the top of " + cfg)
        return 0
    if os.path.exists(cfg):
        shutil.copy2(cfg, cfg + ".reaper-bak")
    with open(cfg, "w") as f:
        f.write(new)
    os.chmod(cfg, 0o600)
    print("  + ssh includes placed at the top of " + cfg + " (previous: " + cfg + ".reaper-bak)")
    for w in wanted:
        print("      Include " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
