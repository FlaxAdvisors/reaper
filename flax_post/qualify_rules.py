# flax_post/qualify_rules.py
"""The Qualify judges shared by loose-node Post (observe/host_qual.py) and the
rack order server (flax_rack/qualify.py). Pure and stdlib-only: text and dicts
in, verdicts out -- no database, no I/O beyond the population profile and
blocklist files -- so rackpost can ship and import it without psycopg.

Moved out of observe/host_qual.py unchanged (rackpost plan 2b, Task 0b);
host_qual re-imports every name, so the loose-node service is unaffected.
"""
from . import blocklist
from . import population
from . import shipchecks

# A blade whose FRU 0 answered without its ship serial (fru.read_baseboard
# state no_serial) must not ship: the same red path as a blocked DIMM
# (spec 2026-09-14-fru-id0-only D6, §6.1).
NO_SHIP_RULE = "no ship serial: FRU 0 %s empty — do not ship"


def population_check(dump, profile, dimmsum=None, baseboard=None) -> dict:
    """green = all profile rules matched, red = any missing, grey = no dump/profile.
    A blocked DIMM (blocklist, ruling 2026-09-12) is red whatever the profile
    says: the hits ride along as `blocked_dimms` and as failed rules.
    baseboard = the ladder's bmc-ready evidence (fru.read_baseboard). A
    no_serial baseboard is red once the run's inventory dump exists, profile or
    not (`no_ship_serial`). A baseboard without a `state` (a row that predates
    FRU-ID-0-only) adds nothing."""
    blocked = blocklist.check_dimmsum(dimmsum) if dimmsum else []
    if not dump or not profile:
        out = {"profile": profile, "verdict": "grey", "failed_rules": []}
    else:
        rules = population.load_profile(profile)
        res = population.evaluate(rules, dump)
        failed = [r["rule"] for r in res["results"] if not r["ok"]]
        out = {"profile": profile, "verdict": "green" if res["ok"] else "red", "failed_rules": failed}
    if blocked:
        out["verdict"] = "red"
        out["failed_rules"] = list(out["failed_rules"]) + [blocklist.describe(h) for h in blocked]
        out["blocked_dimms"] = blocked
    if dump and isinstance(baseboard, dict) and baseboard.get("state") == "no_serial":
        out["verdict"] = "red"
        out["failed_rules"] = list(out["failed_rules"]) + [
            NO_SHIP_RULE % (baseboard.get("serial_field") or "Product Serial")]
        out["no_ship_serial"] = True
    return out


def qualify_verdict(status, pop, checks=None) -> "str | None":
    """pass = node battery passed AND population green AND (when given) every
    ship check passed; fail = any of them failed; else None. checks=None keeps
    the old two-input rule."""
    node = (status or {}).get("verdict")
    popv = (pop or {}).get("verdict")
    cs = [((checks or {}).get(n) or {}).get("status") for n in shipchecks.SHIP_CHECKS]
    if node == "fail" or popv == "red" or (checks is not None and "fail" in cs):
        return "fail"
    if node == "pass" and popv == "green" and (checks is None or all(c == "pass" for c in cs)):
        return "pass"
    return None
