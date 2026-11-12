"""Tests for flax_switch_sense.macmath (loader + classifiers, DB-free)."""
import json

from flax_switch_sense.mac import int_to_mac, mac_to_int
from flax_switch_sense.macmath import (
    classify_offset,
    classify_distinct_oui,
    load_macmath_dir,
)


def test_classify_offset_leopard():
    observed = ["aa:bb:cc:00:00:00", "aa:bb:cc:00:00:02"]
    bmc, hosts, junk = classify_offset(observed, bmc_offset=2, host_offsets=[0])
    assert bmc == "aa:bb:cc:00:00:02"
    assert hosts == ["aa:bb:cc:00:00:00"]
    assert junk == []


def test_classify_offset_yosemite():
    s1 = mac_to_int("aa:bb:cc:00:00:10")
    observed = [int_to_mac(s1 + o) for o in (0, 1, 2, 4, 6)]
    bmc, hosts, junk = classify_offset(
        observed, bmc_offset=1, host_offsets=[0, 2, 4, 6]
    )
    assert bmc == int_to_mac(s1 + 1)
    assert hosts == [int_to_mac(s1 + o) for o in (0, 2, 4, 6)]
    assert junk == []


def test_classify_offset_monolake():
    h = mac_to_int("aa:bb:cc:00:00:20")
    observed = [int_to_mac(h), int_to_mac(h + 1)]
    bmc, hosts, junk = classify_offset(observed, bmc_offset=1, host_offsets=[0])
    assert bmc == int_to_mac(h + 1)
    assert hosts == [int_to_mac(h)]
    assert junk == []


def test_classify_offset_computed_bmc_absent_falls_back():
    base = "aa:bb:cc:00:00:00"
    observed = [base]
    # bmc would be base+2, which is not present -> signal fallback
    assert classify_offset(observed, bmc_offset=2, host_offsets=[0]) == (
        None,
        [],
        [],
    )


def test_classify_distinct_oui_wedge():
    observed = [
        "3c:2c:99:58:9d:00",
        "3c:2c:99:58:9d:02",
        "00:90:fb:61:da:a0",
    ]
    bmc, hosts, junk = classify_distinct_oui(observed)
    assert bmc == "3c:2c:99:58:9d:02"  # highest
    assert hosts == ["00:90:fb:61:da:a0"]  # foreign OUI
    assert junk == [
        {"mac": "3c:2c:99:58:9d:00", "reason": "macmath_same_oui_extra"}
    ]


def test_load_macmath_dir_reads_int_named_configs(tmp_path):
    (tmp_path / "26.json").write_text(
        json.dumps({"scheme": "distinct_oui"})
    )
    (tmp_path / "24.json").write_text(
        json.dumps({"scheme": "offset", "bmc_offset": 2, "host_offsets": [0]})
    )
    # non-int filename -> skipped
    (tmp_path / "bad.json").write_text(json.dumps({"scheme": "offset"}))
    # invalid scheme -> skipped
    (tmp_path / "99.json").write_text(json.dumps({"scheme": "nope"}))

    configs = load_macmath_dir(str(tmp_path))

    assert set(configs) == {24, 26}
    assert configs[26] == {"scheme": "distinct_oui"}
    assert configs[24]["scheme"] == "offset"


def test_load_macmath_dir_absent_dir_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert load_macmath_dir(str(missing)) == {}


def test_load_macmath_dir_skips_malformed(tmp_path):
    (tmp_path / "30.json").write_text("{ this is not json")
    (tmp_path / "31.json").write_text(json.dumps({"scheme": "offset"}))
    configs = load_macmath_dir(str(tmp_path))
    assert set(configs) == {31}
