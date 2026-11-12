from flax_post.biosd import manifest
from flax_post.observe.family_map import compile_family_map_text

_ENTRIES = [
    {"platform": "Wiwynn Tioga Pass", "family": "tiogapass", "target": "TPC_P26C",
     "afulnx_url": "http://bang/x/afulnx_64_Wiwynn_Tioga_Pass",
     "bin_url": "http://bang/x/TPC_P26C.zip", "bin_path": "TPC_P26C/TPC_P26C.bin",
     "flags": ["/X","/B","/P","/K","/N","/ME","/REBOOT"]},
    {"platform": "Quanta Leopard", "family": "leopard", "target": "F06_3B22",
     "afulnx_url": "http://bang/x/afulnx_64_Quanta_LEOPARD_BDW",
     "bin_url": "http://bang/x/leopard.zip", "bin_path": "leopard/leo.bin",
     "flags": ["/p","/b"]},
]

_FM = {"tiogapass": compile_family_map_text(
           "SV7220G3\nTiogaPass\n^Tioga Pass( Single Side)?$\n"),
       "leopard": compile_family_map_text("Leopard\n")}


def _dmi(product):
    return ("Handle 0x0001, DMI type 1, 27 bytes\nSystem Information\n"
            "\tManufacturer: Wiwynn\n\tProduct Name: %s\n\tVersion: Default string\n" % product)


def test_match_resolves_the_dmi_product_through_the_family_map():
    """The platform is whatever /etc/flax/family-map says the DMI Product Name
    is; the manifest names the FAMILY. Every spelling the family map knows
    reaches the same entry (et24b1 read `TiogaPass`, the rest `SV7220G3`)."""
    m = manifest.BiosMatcher(_ENTRIES, lambda: _FM)
    for product in ("SV7220G3", "TiogaPass", "Tioga Pass"):
        assert m.match(_dmi(product))["target"] == "TPC_P26C", product
    assert m.match(_dmi("Leopard-DDR4"))["target"] == "F06_3B22"
    assert m.match(_dmi("PowerEdge R640")) is None


def test_match_reads_only_the_product_name_line():
    # `Manufacturer: Leopard Inc` must not select the leopard entry.
    m = manifest.BiosMatcher(_ENTRIES, lambda: _FM)
    text = _dmi("PowerEdge").replace("Manufacturer: Wiwynn", "Manufacturer: Leopard Inc")
    assert m.match(text) is None
    assert m.match("") is None and m.match("no product line here") is None


def test_family_without_a_manifest_entry_is_no_match():
    m = manifest.BiosMatcher(_ENTRIES[:1], lambda: _FM)
    assert m.match(_dmi("Leopard-DDR4")) is None


def test_dmi_product():
    assert manifest.dmi_product(_dmi("TiogaPass")) == "TiogaPass"
    assert manifest.dmi_product("") is None

def test_load_missing_dir_returns_empty(tmp_path):
    assert manifest.load_bios_manifest(str(tmp_path)) == []



def test_post_bios_manifest_matches_triage_host_fw_targets():
    """Post biosd and triage bios_fw must expect the SAME BIOS string per family
    (operator 2026-11-06: Leopards were offered the TiogaPass TPO_P26F), at every
    site: since 2026-11-11 (f5e5ee2) post's manifest is rendered per site from
    post_bios_fw_platforms, next to that site's host_fw_platforms."""
    import os, yaml
    from reapercli.lib.inventory import _Loader
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
    for site in ("eindhoven", "lavergne", "braintree"):
        with open(os.path.join(root, "inventories", site, "group_vars", "all", "vars.yml")) as f:
            v = yaml.load(f, Loader=_Loader)
        triage = v["host_fw_platforms"]
        post = {e["family"]: e for e in v["post_bios_fw_platforms"]}
        assert set(triage) == {"tiogapass-bios", "leopard-bios"}, site   # presence: both families pinned
        for name, entry in triage.items():
            fam = name[:-len("-bios")]
            assert post[fam]["target"] == entry["target_version"], (site, fam)
            assert (post[fam].get("updatable", True) is False) == ("flash" not in entry), (site, fam)
