"""Tests for flax_control.ll — IPv6 link-local (EUI-64) helpers.

DB-free, no network. Mirrors tests/test_flax_observe_ll.py and asserts the
known pair from the plan: 1c:34:da:7f:9d:32 -> fe80::1e34:daff:fe7f:9d32.
"""
import pytest

from flax_control import ll


# ---------------------------------------------------------------------------
# eui64_ll_from_mac
# ---------------------------------------------------------------------------

def test_eui64_ll_known_pair():
    """Plan's anchor pair: 0x1c ^ 0x02 = 0x1e."""
    assert ll.eui64_ll_from_mac("1c:34:da:7f:9d:32") == "fe80::1e34:daff:fe7f:9d32"


def test_eui64_ll_wedge100s():
    assert ll.eui64_ll_from_mac("3c:2c:99:58:9d:02") == "fe80::3e2c:99ff:fe58:9d02"


def test_eui64_ll_mixed_case_and_dashes():
    assert ll.eui64_ll_from_mac("1C-34-DA-7F-9D-32") == "fe80::1e34:daff:fe7f:9d32"


def test_eui64_ll_matches_flax_observe():
    """flax_control.ll must agree byte-for-byte with the flax_observe source."""
    from flax_observe import ll as observe_ll
    for mac in ("1c:34:da:7f:9d:32", "00:90:fb:61:da:a0", "aa:bb:cc:dd:ee:ff"):
        assert ll.eui64_ll_from_mac(mac) == observe_ll.eui64_ll_from_mac(mac)


def test_eui64_ll_bad_mac_raises():
    with pytest.raises(ValueError):
        ll.eui64_ll_from_mac("not:a:mac")


def test_eui64_ll_too_short_raises():
    with pytest.raises(ValueError):
        ll.eui64_ll_from_mac("aa:bb:cc:dd:ee")


# ---------------------------------------------------------------------------
# ll_with_zone
# ---------------------------------------------------------------------------

def test_ll_with_zone_appends_parent_dot_vid():
    assert (ll.ll_with_zone("1c:34:da:7f:9d:32", 17)
            == "fe80::1e34:daff:fe7f:9d32%eth0.17")


def test_ll_with_zone_custom_parent():
    assert (ll.ll_with_zone("1c:34:da:7f:9d:32", 26, parent="eth1")
            == "fe80::1e34:daff:fe7f:9d32%eth1.26")


def test_ll_with_zone_vid_as_string():
    assert (ll.ll_with_zone("1c:34:da:7f:9d:32", "17")
            == "fe80::1e34:daff:fe7f:9d32%eth0.17")


def test_ll_with_zone_no_vid_uses_bare_parent():
    """A falsy vid yields the bare parent interface (no sub-interface suffix)."""
    assert (ll.ll_with_zone("1c:34:da:7f:9d:32", None)
            == "fe80::1e34:daff:fe7f:9d32%eth0")
