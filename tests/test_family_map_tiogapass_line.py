"""D4 (operator 2026-09-18): tiogapass.txt gains ^Tioga Pass( Single Side)?$.
Covers the Quanta board (Product Name 'Tioga Pass Single Side') and the
Wistron et23b2 ('Tioga Pass'); nothing else may change family.

Controller ruling (pre-flight F1, overrides the brief's single shared
EXPECTED dict): braintree's brycecanyon.txt has no BryceCanyon line
(eindhoven's does), so expected families are per-site. braintree asserts
only the product names its own family-map files define; braintree's
tiogapass.txt (and every other family-map file besides the D4 line) is
left untouched.
"""
import hashlib
import pathlib

import pytest

import flax_discover
from flax_discover.family_map import compile_family_map_text, load_family_map_dir, match_family

ROOT = pathlib.Path(flax_discover.__file__).resolve().parents[1]
SITES = ("eindhoven", "braintree")
D4 = "^Tioga Pass( Single Side)?$"
# Every product name live in observe_state on bang-gouda + bang-fiesta
# (2026-09-18), plus the FRU fixtures' names and near-misses.
_COMMON = {
    "Tioga Pass": "tiogapass",
    "Tioga Pass Single Side": "tiogapass",
    "Type3 Tioga Pass Single Side": "tiogapass",
    "Type6 Tioga Pass Single Side": "tiogapass",
    "Type8 Tioga Pass Single Side": "tiogapass",
    "Capri2": "capri",
    "Leopard ORv2-DDR4": "leopard",
    "WEDGE100S12V": "wedge100s",
    "Mono Lake-M.2 SATA": None,
    "TS2290-E7278P-6SSD": None,
    "HPE Eth 10/25Gb 2p 640SFP28 Adptr": None,
    "Ava-M.2-SSD-Adapter": None,
    "Tioga Pass Foo": None,
    "AMI Redfish Server": None,
}
EXPECTED = {
    # eindhoven's brycecanyon.txt carries a BryceCanyon line.
    "eindhoven": dict(_COMMON, **{"BryceCanyon": "brycecanyon"}),
    # braintree's brycecanyon.txt is comment-only (pre-flight F1 finding):
    # no line, so BryceCanyon resolves to no family there.
    "braintree": dict(_COMMON, **{"BryceCanyon": None}),
}


def _dir(site):
    return ROOT / "files" / "configs" / site / "services" / "family-map"


@pytest.mark.parametrize("site", SITES)
def test_every_known_product_name_gets_its_family(site):
    fm = load_family_map_dir(str(_dir(site)))
    for pn, fam in EXPECTED[site].items():
        assert match_family(fm, pn) == fam, (site, pn)


@pytest.mark.parametrize("site", SITES)
def test_the_d4_line_changes_only_the_two_bare_tioga_names(site):
    fm = load_family_map_dir(str(_dir(site)))
    text = (_dir(site) / "tiogapass.txt").read_text()
    assert D4 in text.splitlines()
    old = dict(fm, tiogapass=compile_family_map_text(
        "\n".join(l for l in text.splitlines() if l.strip() != D4)))
    changed = {pn for pn in EXPECTED[site] if match_family(old, pn) != match_family(fm, pn)}
    assert changed == {"Tioga Pass", "Tioga Pass Single Side"}


def test_site_copies_are_identical():
    md5 = {s: hashlib.md5((_dir(s) / "tiogapass.txt").read_bytes()).hexdigest() for s in SITES}
    assert md5["eindhoven"] == md5["braintree"]
