"""Tests for flax_classify.formula — lifted from reaper_leased.alloc_ip."""

import pytest


# --- alloc_ip ---

def test_alloc_ip_rabbit_host():
    from flax_classify.formula import alloc_ip
    assert alloc_ip("rabbit-lorax", "Et6/1", 17, "host") == "172.17.6.1"


def test_alloc_ip_rabbit_bmc_adds_100():
    from flax_classify.formula import alloc_ip
    assert alloc_ip("rabbit-lorax", "Et6/1", 17, "bmc") == "172.17.6.101"


def test_alloc_ip_rabbit_vm_uses_200_plus_n():
    from flax_classify.formula import alloc_ip
    assert alloc_ip("rabbit-lorax", "Et6/1", 17, "vm", vm_n=1) == "172.17.6.201"
    assert alloc_ip("rabbit-lorax", "Et6/4", 17, "vm", vm_n=3) == "172.17.6.203"


def test_alloc_ip_rabbit_internal_port_form():
    """et6b1 short form is accepted (matches flax-observe geometry shape)."""
    from flax_classify.formula import alloc_ip
    assert alloc_ip("rabbit-lorax", "et6b1", 17, "host") == "172.17.6.1"
    assert alloc_ip("rabbit-lorax", "et6b1", 17, "bmc") == "172.17.6.101"


def test_alloc_ip_turtle_host():
    from flax_classify.formula import alloc_ip
    assert alloc_ip("turtle-fudge", "swp6", 24, "host") == "172.24.0.6"


def test_alloc_ip_turtle_bmc_adds_100():
    from flax_classify.formula import alloc_ip
    assert alloc_ip("turtle-fudge", "swp6", 24, "bmc") == "172.24.0.106"


def test_alloc_ip_turtle_infra_slot_raises():
    from flax_classify.formula import alloc_ip
    with pytest.raises(ValueError, match="collides with infra slot"):
        alloc_ip("turtle-fudge", "swp3", 24, "host")


def test_alloc_ip_vm_without_n_raises():
    from flax_classify.formula import alloc_ip
    with pytest.raises(ValueError, match="vm_n"):
        alloc_ip("rabbit-lorax", "Et6/1", 17, "vm")


def test_alloc_ip_unknown_kind_raises():
    from flax_classify.formula import alloc_ip
    with pytest.raises(ValueError, match="unsupported kind"):
        alloc_ip("rabbit-lorax", "Et6/1", 17, "bogus")


def test_alloc_ip_unknown_switch_raises():
    from flax_classify.formula import alloc_ip
    with pytest.raises(ValueError, match="unsupported switch"):
        alloc_ip("hippo-foo", "Et6/1", 17, "host")


# --- port_token ---

def test_port_token_rabbit_long_form():
    from flax_classify.formula import port_token
    assert port_token("Ethernet6/3") == "6b3"


def test_port_token_rabbit_short_form_passthrough():
    from flax_classify.formula import port_token
    assert port_token("Et6/3") == "6b3"


def test_port_token_rabbit_internal_form_passthrough():
    from flax_classify.formula import port_token
    assert port_token("et6b3") == "et6b3"  # already internal; lowered


def test_port_token_turtle_swp():
    from flax_classify.formula import port_token
    assert port_token("swp6") == "swp6"


# --- classify_one ---

def test_classify_one_rabbit_bmc():
    from flax_classify.formula import classify_one
    r = classify_one("rabbit-lorax", "Et6/1", "1c:34:da:7f:b3:a4", "bmc", 17)
    assert r == {"ipv4_address": "172.17.6.101",
                 "ipv6_address": "fd00:17::6:101",
                 "hostname": "unknown6b1-bmc"}


def test_classify_one_rabbit_host():
    from flax_classify.formula import classify_one
    r = classify_one("rabbit-lorax", "Et6/1", "1c:34:da:7f:b3:a2", "host", 17)
    assert r == {"ipv4_address": "172.17.6.1",
                 "ipv6_address": "fd00:17::6:1",
                 "hostname": "unknown6b1"}


def test_classify_one_turtle_host():
    from flax_classify.formula import classify_one
    r = classify_one("turtle-fudge", "swp6", "aa:bb:cc:dd:ee:ff", "host", 24)
    assert r == {"ipv4_address": "172.24.0.6",
                 "ipv6_address": "fd00:24::6",
                 "hostname": "unknownswp6"}


def test_classify_one_accepts_family_override():
    """Plan 5 will feed real family from devices.latched. Plan 4 uses
    'unknown' default; pass-through is needed for that future call."""
    from flax_classify.formula import classify_one
    r = classify_one("rabbit-lorax", "Et6/1", "1c:34:da:7f:b3:a4", "bmc", 17,
                     family="leopard")
    assert r["hostname"] == "leopard6b1-bmc"


# --- alloc_ip6 ---

def test_alloc_ip6_rabbit_host_bmc_vm():
    from flax_classify.formula import alloc_ip6
    assert alloc_ip6("rabbit-lorax", "Et7/1", 16, "host") == "fd00:16::7:1"
    assert alloc_ip6("rabbit-lorax", "Et7/1", 16, "bmc") == "fd00:16::7:101"
    assert alloc_ip6("rabbit-lorax", "Et7/1", 16, "vm", vm_n=1) == "fd00:16::7:201"
    # internal et-form parses the same as Arista form
    assert alloc_ip6("rabbit-lorax", "et7b1", 16, "host") == "fd00:16::7:1"


def test_alloc_ip6_turtle_collapses_zero_third_octet():
    from flax_classify.formula import alloc_ip6
    # turtle 3rd octet is 0 -> canonical IPv6 collapses it
    assert alloc_ip6("turtle-lorax", "swp8", 17, "host") == "fd00:17::8"
    assert alloc_ip6("turtle-lorax", "swp8", 17, "bmc") == "fd00:17::108"


def test_alloc_ip6_matches_v4_octets():
    from flax_classify.formula import alloc_ip, alloc_ip6
    import ipaddress
    v4 = alloc_ip("rabbit-lorax", "Et9/3", 24, "bmc")     # 172.24.9.103
    v6 = alloc_ip6("rabbit-lorax", "Et9/3", 24, "bmc")    # fd00:24::9:103
    o3, o4 = v4.split(".")[2:]
    assert v6 == str(ipaddress.IPv6Address(f"fd00:24::{o3}:{o4}"))


def test_classify_one_includes_ipv6():
    from flax_classify.formula import classify_one
    r = classify_one("rabbit-lorax", "Et7/1", "aa:bb:cc:dd:ee:ff", "host", 16,
                     family="leopard")
    assert r["ipv4_address"] == "172.16.7.1"
    assert r["ipv6_address"] == "fd00:16::7:1"
    assert r["hostname"] == "leopard7b1"
