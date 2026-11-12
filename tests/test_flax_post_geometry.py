# tests/test_flax_post_geometry.py
"""Config-driven rack geometry: parse/normalize slots, rack tags, arista form."""
import json
from flax_post import geometry

_SLOT = {"port": "et6b1", "switch": "rabbit-edam",
         "bottom_ou": 12, "height": 2, "width": 3, "pos": "2:3", "chassis": 2}


def test_parse_normalizes_slot():
    geo = geometry.parse_geometry({"racks": [{"switch": "rabbit-edam", "tag": "", "label": "Rack A"}],
                                   "slots": [_SLOT]})
    s = geo["slots"][0]
    assert s["ou"] == 12 and s["group"] == 2
    assert s["col"] == "C"                       # pos 2:3 -> Center
    assert geo["racks"]["rabbit-edam"]["tag"] == ""


def test_bare_list_back_compat():
    geo = geometry.parse_geometry([_SLOT])        # legacy: bare slot list = one rack tag ""
    assert geo["slots"][0]["port"] == "et6b1"
    assert geometry.rack_tag(geo, "rabbit-edam") == ""


def test_col_label():
    assert geometry.col_label("1:3", 3) == "L"
    assert geometry.col_label("3:3", 3) == "R"
    assert geometry.col_label("1:2", 2) == "L"
    assert geometry.col_label("4:4", 4) == "D"
    assert geometry.col_label("1:1", 1) == "full"


def test_to_arista():
    assert geometry.to_arista("et6b1") == "Ethernet6/1"
    assert geometry.to_arista("swp6") == "swp6"


def test_rack_tag_second_rack():
    geo = geometry.parse_geometry({"racks": [{"switch": "rabbit-edam", "tag": ""},
                                             {"switch": "rabbit-x", "tag": "b"}],
                                   "slots": [_SLOT]})
    assert geometry.rack_tag(geo, "rabbit-x") == "b"
    assert geometry.rack_tag(geo, "unknown") == ""    # default empty


def test_load_geometry_reads_file(tmp_path, monkeypatch):
    p = tmp_path / "post-geometry.json"
    p.write_text(json.dumps({"racks": [{"switch": "rabbit-edam", "tag": ""}], "slots": [_SLOT]}))
    monkeypatch.setenv("FLAX_POST_GEOMETRY", str(p))
    import importlib
    importlib.reload(geometry)
    geo = geometry.load_geometry()
    assert len(geo["slots"]) == 1
    assert geometry.load_geometry("/nonexistent/x.json") == {"racks": {}, "slots": []}
