from flax_discover.family_map import compile_family_map_text, match_family


def test_compile_skips_comments_and_blanks():
    pats = compile_family_map_text("# c\n\n  Leopard-DDR4\nTiogaPass\n")
    assert len(pats) == 2


def test_match_first_wins():
    fm = {"leopard": compile_family_map_text("Leopard-DDR4"),
          "tiogapass": compile_family_map_text("TiogaPass")}
    assert match_family(fm, "Leopard-DDR4 rev2") == "leopard"
    assert match_family(fm, "TiogaPass") == "tiogapass"


def test_match_none_when_no_product_name_or_no_match():
    fm = {"leopard": compile_family_map_text("Leopard-DDR4")}
    assert match_family(fm, None) is None
    assert match_family(fm, "") is None
    assert match_family(fm, "SomethingElse") is None


def test_capri_product_names_resolve_to_capri_family():
    fm = {"capri": compile_family_map_text("Capri2\n^Capri\n")}
    assert match_family(fm, "Capri2") == "capri"
    assert match_family(fm, "Capri2.0") == "capri"
    assert match_family(fm, "Capri3") == "capri"
    assert match_family(fm, "SomethingElse") is None
