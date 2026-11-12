"""Tests for flax_observe.geometry."""
import json
import tempfile
from pathlib import Path


def test_load_geometry_returns_list():
    from flax_observe.geometry import load_geometry
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "geometry.json"
        path.write_text(json.dumps([
            {"port": "Et6/1", "ou": "20", "switch": "rabbit-lorax"},
            {"port": "Et6/2", "ou": "22", "switch": "rabbit-lorax"},
        ]))
        out = load_geometry(str(path))
    assert len(out) == 2
    assert out[0]["port"] == "Et6/1"
    assert out[0]["switch"] == "rabbit-lorax"


def test_load_geometry_rejects_missing_keys():
    import pytest
    from flax_observe.geometry import load_geometry, ConfigError
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.json"
        path.write_text(json.dumps([{"port": "Et6/1"}]))   # missing ou + switch
        with pytest.raises(ConfigError):
            load_geometry(str(path))


def test_load_geometry_applies_default_switch():
    from flax_observe.geometry import load_geometry
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "geom.json"
        path.write_text(json.dumps([{"port": "Et6/1", "ou": "20"}]))
        out = load_geometry(str(path), default_switch_name="rabbit-lorax")
    assert out[0]["switch"] == "rabbit-lorax"


# --- turtle-geometry merge tests (via __main__ argument parsing logic) ---

def _make_argv(tmp, geometry_data, turtle_data=None, turtle_path=None):
    """Write temp geometry files and return argv list for main()."""
    geo_path = Path(tmp) / "geometry.json"
    geo_path.write_text(json.dumps(geometry_data))
    argv = [
        "--geometry", str(geo_path),
        "--credentials", "/dev/null",
        "--bmc-credentials", "/dev/null",
        "--host-credentials", "/dev/null",
    ]
    if turtle_path is not None:
        argv += ["--turtle-geometry", str(turtle_path)]
    elif turtle_data is not None:
        tg_path = Path(tmp) / "turtle-geometry.json"
        tg_path.write_text(json.dumps(turtle_data))
        argv += ["--turtle-geometry", str(tg_path)]
    return argv


def _load_merged(argv):
    """Run just the geometry-loading portion of main() and return both lists."""
    import argparse
    import os
    from flax_observe.geometry import load_geometry

    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", default="/etc/flax/geometry.json")
    parser.add_argument("--turtle-geometry", default="/etc/flax/turtle-geometry.json")
    parser.add_argument("--credentials")
    parser.add_argument("--bmc-credentials")
    parser.add_argument("--host-credentials")
    parser.add_argument("--default-switch", default="rabbit-lorax")
    args = parser.parse_args(argv)

    geometry = load_geometry(args.geometry, default_switch_name=args.default_switch)

    turtle_entries: list = []
    tg_path = args.turtle_geometry
    if os.path.exists(tg_path) and not os.path.isdir(tg_path):
        turtle_entries = load_geometry(tg_path)

    return geometry + turtle_entries, len(turtle_entries)


def test_turtle_geometry_merges_into_main():
    """Both main and turtle geometry entries appear in the merged list."""
    with tempfile.TemporaryDirectory() as tmp:
        rabbit_data = [{"port": "Et6/1", "ou": "10", "switch": "rabbit-lorax"}]
        turtle_data = [
            {"port": "swp12", "ou": "T12", "switch": "turtle-lorax"},
            {"port": "swp13", "ou": "T13", "switch": "turtle-lorax"},
        ]
        argv = _make_argv(tmp, rabbit_data, turtle_data=turtle_data)
        merged, turtle_count = _load_merged(argv)

    assert len(merged) == 3
    assert turtle_count == 2
    switches = [e["switch"] for e in merged]
    assert "rabbit-lorax" in switches
    assert switches.count("turtle-lorax") == 2
    ports = [e["port"] for e in merged]
    assert "Et6/1" in ports
    assert "swp12" in ports
    assert "swp13" in ports


def test_missing_turtle_geometry_returns_main_only():
    """A non-existent turtle-geometry path → no crash, only main geometry returned."""
    with tempfile.TemporaryDirectory() as tmp:
        rabbit_data = [{"port": "Et6/1", "ou": "10", "switch": "rabbit-lorax"}]
        missing_path = Path(tmp) / "no-such-file.json"
        argv = _make_argv(tmp, rabbit_data, turtle_path=str(missing_path))
        merged, turtle_count = _load_merged(argv)

    assert len(merged) == 1
    assert turtle_count == 0
    assert merged[0]["switch"] == "rabbit-lorax"


def test_turtle_geometry_dir_path_treated_as_absent():
    """A directory at the turtle-geometry path is treated as absent (no crash)."""
    with tempfile.TemporaryDirectory() as tmp:
        rabbit_data = [{"port": "Et6/1", "ou": "10", "switch": "rabbit-lorax"}]
        # Pass the tmp dir itself as the turtle-geometry path — it is a directory
        argv = _make_argv(tmp, rabbit_data, turtle_path=tmp)
        merged, turtle_count = _load_merged(argv)

    assert len(merged) == 1
    assert turtle_count == 0
