"""Zone A must be opted out of bmc_fw and bios_fw, and mezz_flash must cover
exactly Zone A.

The dangerous drift here is SILENT: a station slot missing from
BMC_FW_DISABLE_PORTS means bmc_fw flashes a blade in the middle of an unlock
run -- and under BMC_FW_POWER_ON=true it can draw a power-on on a slot whose
identity changed because someone swapped the card by hand.

Since Phase 5 (spec 2026-11-08-reaper-triage-deploy-design) the lists live in
each site's group_vars as apply_triage_compose_env, rendered into
/etc/flax/triage/.env by `reaper deploy <site> triage`; the triage repo's
compose reads them as ${VAR?}.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reapercli.lib.inventory import _group_vars  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

KEYS = ("POWER_INTERLOCK_SKIP_PORTS", "MEZZ_FLASH_PORTS", "BMC_FW_DISABLE_PORTS", "BIOS_FW_DISABLE_PORTS")
ZONE_A = {f"et{c}b{b}" for c in (5, 9, 27) for b in (1, 2, 3, 4)}
SITES = load_sites(ROOT)


def _env(site):
    return _group_vars(SITES[site], "all").get("apply_triage_compose_env") or {}


def _sets(site):
    return {k: {p.strip() for p in str(_env(site)[k]).split(",") if p.strip()} for k in KEYS}


def test_every_triage_site_sets_the_four_lists():
    for site in ("eindhoven", "lavergne", "braintree"):
        env = _env(site)
        assert set(KEYS) <= set(env), site
        assert all(isinstance(env[k], str) for k in KEYS), site
    for site in ("eindhoven", "lavergne"):
        assert all(_env(site)[k] for k in KEYS), site


def test_lists_agree_within_a_site():
    for site in ("eindhoven", "lavergne", "braintree"):
        sets = _sets(site)
        assert len({frozenset(s) for s in sets.values()}) == 1, (site, sets)


def test_eindhoven_is_zone_a():
    assert _sets("eindhoven")["MEZZ_FLASH_PORTS"] == ZONE_A


def test_lavergne_is_et25():
    assert _sets("lavergne")["MEZZ_FLASH_PORTS"] == {f"et25b{b}" for b in (1, 2, 3, 4)}


def test_braintree_is_empty():
    # operator 2026-11-08: braintree has no station ports
    assert all(_env("braintree")[k] == "" for k in KEYS)
