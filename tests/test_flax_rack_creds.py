"""flax_rack.creds: host (flax row) + redfish lists are optional."""
import json
import pytest
from flax_rack.creds import load_creds, CredsError


def _w(p, obj):
    p.write_text(json.dumps(obj))
    return str(p)


def _base(tmp_path):
    return (_w(tmp_path / "m.json", {"sonicuser": "su", "sonicpass": "sp"}),
            _w(tmp_path / "b.json", [{"bmcuser": "bu", "bmcpass": "bp"}]))


def test_flax_row_chosen_from_three(tmp_path):
    m, b = _base(tmp_path)
    h = _w(tmp_path / "h.json", [{"user": "root", "pass": "a"}, {"user": "flax", "pass": "f"},
                                 {"user": "admin", "pass": "c"}])
    c = load_creds(m, b, h, str(tmp_path / "nope.json"))
    assert c.host == ("flax", "f")
    assert c.redfish == []
    assert c.switch == {"sonicuser": "su", "sonicpass": "sp"} and c.bmc == [("bu", "bp")]


def test_missing_host_file_is_empty(tmp_path):
    m, b = _base(tmp_path)
    c = load_creds(m, b, str(tmp_path / "x"), str(tmp_path / "y"))
    assert c.host == () and c.redfish == []


def test_no_flax_row_is_empty(tmp_path):
    m, b = _base(tmp_path)
    h = _w(tmp_path / "h.json", [{"user": "root", "pass": "a"}])
    assert load_creds(m, b, h, str(tmp_path / "y")).host == ()


def test_redfish_parsed(tmp_path):
    m, b = _base(tmp_path)
    r = _w(tmp_path / "r.json", [{"rfuser": "u1", "rfpass": "p1"}, {"rfuser": "u2", "rfpass": "p2"},
                                 {"junk": 1}])
    c = load_creds(m, b, str(tmp_path / "x"), r)
    assert c.redfish == [("u1", "p1"), ("u2", "p2")]


def test_main_still_required(tmp_path):
    with pytest.raises(CredsError):
        load_creds(str(tmp_path / "none"), str(tmp_path / "none2"))


def test_unknown_nos_is_warned_and_skipped_not_fatal(tmp_path, caplog):
    m, b = _base(tmp_path)
    with caplog.at_level("WARNING", logger="flax_rack.creds"):
        c = load_creds(m, b, str(tmp_path / "x"), str(tmp_path / "y"),
                       noses=("sonic", "junos"))
    assert c.switch == {"sonicuser": "su", "sonicpass": "sp"} and c.has_switch("sonic")
    assert "junos" in caplog.text


def test_known_nos_without_its_login_still_fails(tmp_path):
    m, b = _base(tmp_path)
    with pytest.raises(CredsError, match="eosuser"):
        load_creds(m, b, str(tmp_path / "x"), str(tmp_path / "y"), noses=("eos", "junos"))
