"""PostMatcher: the post rack is one platform — match by the bmc_active entry, not product name."""
from flax_post.fwd import manifest

_MANIFEST = {
    "tiogapass-openbmc": {
        "match": {"product_name": ["(?i)tioga"], "bmc_kind": "openbmc"},
        "target_version": "flax-onetree-1.0.7-202606261820",
        "check": {"method": "redfish-firmware-inventory", "id": "bmc_active"},
        "flash": {"method": "redfish-update-multipart",
                  "artifact": "SXC-Lab_OCP-Updates/OpenBMC/fx-tp-1.0.7-202606261820.mtd.tar"},
    }
}


def test_post_matcher_ignores_product_name():
    pm = manifest.PostMatcher(_MANIFEST)
    # post BMCs return product_name None (Redfish Model is None) — still matches.
    for product in (None, "", "anything"):
        platform, entry = pm.match(product)
        assert platform == "tiogapass-openbmc"
        assert entry["check"]["id"] == "bmc_active"
        assert entry["target_version"] == "flax-onetree-1.0.7-202606261820"


def test_post_matcher_no_bmc_active_entry_is_none():
    assert manifest.PostMatcher({}).match(None) is None
    assert manifest.PostMatcher({"x": {"check": {"id": "other"}}}).match(None) is None


_OEM_MANIFEST = {
    "tiogapass-openbmc": _MANIFEST["tiogapass-openbmc"],
    "tiogapass-ami-oem": {
        "match": {"bmc_kind": "redfish", "product_name": [r"(?i)ami\s*redfish", r"(?i)^wiwynn$"]},
        "updatable": False,
    },
}


def test_match_oem_matches_updatable_false_entry():
    pm = manifest.PostMatcher(_OEM_MANIFEST)
    for product in ("AMI Redfish Server", "Ami Redfish", "Wiwynn"):
        hit = pm.match_oem(product)
        assert hit is not None and hit[0] == "tiogapass-ami-oem"


def test_match_oem_ignores_onetree_product_and_empty():
    pm = manifest.PostMatcher(_OEM_MANIFEST)
    # onetree boards ('Wiwynn Tioga Pass') must NOT match the ^wiwynn$-anchored OEM entry.
    assert pm.match_oem("Wiwynn Tioga Pass") is None
    assert pm.match_oem(None) is None
    assert pm.match_oem("") is None


def test_match_oem_none_when_no_updatable_false_entry():
    # A pure-onetree manifest (eindhoven) exposes no OEM entry -> never an OEM match.
    assert manifest.PostMatcher(_MANIFEST).match_oem("AMI Redfish Server") is None
