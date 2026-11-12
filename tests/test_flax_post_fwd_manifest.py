"""flax_post.fwd.manifest: product_name matcher.

Version comparison moved to flax_post.fwd.version (triage parity) and is
re-exported by manifest; its contract is pinned in
test_flax_post_version_parity.py."""
import json
import pytest

_ENTRY = {
    "match": {"product_name": ["(?i)tioga", "(?i)tp[-_ ]?"], "bmc_kind": "openbmc"},
    "target_version": "flax-onetree-1.0.6-202606261452",
    "check": {"method": "redfish-firmware-inventory", "id": "bmc_active"},
    "flash": {"method": "redfish-update-multipart",
              "artifact": "SXC-Lab_OCP-Updates/OpenBMC/fx-tp-1.0.6-202606261452.mtd.tar"},
    "auto": True,
}
_MANIFEST = {"tiogapass-openbmc": _ENTRY}


def test_matcher_hits_on_product_regex():
    from flax_post.fwd.manifest import Matcher
    m = Matcher(_MANIFEST)
    hit = m.match("Tioga Pass S2T")
    assert hit is not None
    platform, entry = hit
    assert platform == "tiogapass-openbmc"
    assert entry["target_version"] == "flax-onetree-1.0.6-202606261452"


def test_matcher_misses_unknown_and_none():
    from flax_post.fwd.manifest import Matcher
    m = Matcher(_MANIFEST)
    assert m.match("SuperMicro X11") is None
    assert m.match(None) is None
    assert m.match("") is None


def test_accessors():
    from flax_post.fwd import manifest
    assert manifest.target_version(_ENTRY) == "flax-onetree-1.0.6-202606261452"
    assert manifest.check_id(_ENTRY) == "bmc_active"
    assert manifest.artifact_rel_path(_ENTRY).endswith(".mtd.tar")


def test_load_manifest_missing_returns_empty(tmp_path):
    from flax_post.fwd import manifest
    assert manifest.load_manifest(str(tmp_path)) == {}


def test_load_manifest_reads_file(tmp_path):
    (tmp_path / "bmc-firmware-versions.json").write_text(json.dumps(_MANIFEST))
    from flax_post.fwd import manifest
    assert "tiogapass-openbmc" in manifest.load_manifest(str(tmp_path))


def test_load_manifest_non_dict_returns_empty(tmp_path):
    (tmp_path / "bmc-firmware-versions.json").write_text("[1, 2, 3]")
    from flax_post.fwd import manifest
    assert manifest.load_manifest(str(tmp_path)) == {}


def test_compare_is_re_exported_from_the_version_module():
    """manifest.compare stays importable for existing call sites, but the
    semantics are version.py's (same|differs). Full contract lives in
    test_flax_post_version_parity.py."""
    from flax_post.fwd import manifest, version
    assert manifest.compare is version.compare
    assert manifest.needs_update is version.needs_update
