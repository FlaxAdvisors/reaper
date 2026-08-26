"""Parity-capture CLI: role_registry.resolve_role vs legacy vlan_policy.phase_for.

The phase-1 deploy gate artifact (docs/refactor-phase-plan.md Phase 1):
"behavior-identical" means resolve_role() must return exactly what
phase_for() returns for every (switch, port) present in switch_facts. Run
as `python -m flax_classify.role_registry_parity` against the real roles.d
+ geometry.json + switch_facts before cutting flax-classify over to the
registry as the source of truth.

compare() is pure (no DB import at module scope) so it can be unit-tested
without Postgres; main() only touches the DB when --facts-json is absent.
"""
import argparse
import copy
import json
import sys

from .role_registry import load_role_dir, resolve_role, validate_roles
from .vlan_policy import load_phase_geometry, phase_for


def compare(facts: dict, geom_tokens: set, defs: dict) -> dict:
    """Compare legacy phase_for() vs registry resolve_role() over every
    (switch, port) in ``facts`` (switch_facts shape:
    {switch: {"ports": {port: {...}}}}).

    Returns {"total": int, "mismatches": [(switch, port, legacy, new)],
    "would_be_unassigned": [(switch, port)]}. would_be_unassigned lists
    ports whose registry resolution depends solely on a catch_all role:
    re-resolving against a deep copy of ``defs`` with every
    universe["catch_all"] forced False yields None for these ports.
    """
    defs_no_catch_all = copy.deepcopy(defs)
    for d in defs_no_catch_all.values():
        d.setdefault("universe", {})["catch_all"] = False

    total = 0
    skipped_non_access = 0
    mismatches = []
    would_be_unassigned = []
    for switch, sdata in facts.items():
        for port, info in (sdata.get("ports") or {}).items():
            # Only access ports participate in DHCP/vid role work (operator
            # directive 2026-07-04): uplinks/trunks/Cpu are outside every
            # role's lens, so they are neither compared nor reported.
            if (info or {}).get("mask") != "access":
                skipped_non_access += 1
                continue
            total += 1
            legacy = phase_for(geom_tokens, switch, port)
            new = resolve_role(defs, switch, port)
            if legacy != new:
                mismatches.append((switch, port, legacy, new))
            if resolve_role(defs_no_catch_all, switch, port) is None:
                would_be_unassigned.append((switch, port))
    return {"total": total, "skipped_non_access": skipped_non_access,
            "mismatches": mismatches,
            "would_be_unassigned": would_be_unassigned}


def _print_report(result: dict) -> None:
    print(f"total access ports compared: {result['total']} "
          f"(skipped non-access: {result['skipped_non_access']})")
    if not result["mismatches"]:
        print("PARITY OK")
    else:
        print(f"MISMATCHES ({len(result['mismatches'])}):")
        print(f"{'switch':<24}{'port':<12}{'legacy':<10}{'new':<10}")
        for sw, port, legacy, new in result["mismatches"]:
            print(f"{sw:<24}{port:<12}{str(legacy):<10}{str(new):<10}")
    wbu = result["would_be_unassigned"]
    print(f"would-be-unassigned ({len(wbu)}):")
    for sw, port in wbu:
        print(f"  {sw} {port}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="flax-classify-role-registry-parity",
        description="Compare role_registry.resolve_role() against legacy "
                    "vlan_policy.phase_for() over switch_facts (phase-1 "
                    "deploy gate).")
    ap.add_argument("--roles-dir", required=True, help="roles.d directory")
    ap.add_argument("--geometry", required=True, help="geometry.json path")
    ap.add_argument("--facts-json", metavar="FILE",
                    help="switch_facts-shaped JSON snapshot to compare "
                         "against; omit to read live switch_facts from the "
                         "DB (libpq env)")
    args = ap.parse_args(argv)

    defs = load_role_dir(args.roles_dir)
    validate_roles(defs)
    geom_tokens = load_phase_geometry(args.geometry)

    if args.facts_json:
        with open(args.facts_json) as f:
            facts = json.load(f)
    else:
        # Local import: keep the pure compare()/CLI-parse path free of a
        # psycopg dependency when --facts-json is given.
        from .__main__ import _build_conninfo
        from .db import build_pool, read_switch_facts
        pool = build_pool(_build_conninfo())
        facts = read_switch_facts(pool)

    result = compare(facts, geom_tokens, defs)
    _print_report(result)
    return 0 if not result["mismatches"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
