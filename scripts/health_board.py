#!/usr/bin/env python3
"""Generate one site's health board artefacts (reapercli/healthboard.py):
    scripts/health_board.py --site eindhoven --out /tmp/hb --domain <bang_domain>
    scripts/health_board.py --unit-include      (node_exporter's unit regex only)
Run from anywhere; the reaper root is this script's parent's parent."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reapercli.healthboard import write_site  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="health_board.py")
    p.add_argument("--site")
    p.add_argument("--out", type=Path)
    p.add_argument("--domain", default="",
                   help="the lab domain (bang_domain) for links to other sites' boards")
    p.add_argument("--unit-include", action="store_true",
                   help="print node_exporter's systemd unit-include regex and exit")
    a = p.parse_args(argv)
    if a.unit_include:
        from reapercli.healthboard import unit_include_regex
        from reapercli.lib.components import load_components
        from reapercli.lib.sites import load_sites
        print(unit_include_regex(load_components(ROOT, load_sites(ROOT))))
        return 0
    if not (a.site and a.out):
        p.error("--site and --out are required")
    for path in write_site(ROOT, a.site, a.out, a.domain):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
