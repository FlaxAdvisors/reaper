"""Tests for flax_observe.ll — DB-free, no real network."""
import pytest

from flax_observe import ll
from flax_reconcile.kick import _eui64_ll_from_mac as _reconcile_eui64


# ---------------------------------------------------------------------------
# eui64_ll_from_mac
# ---------------------------------------------------------------------------

def test_eui64_ll_wedge100s():
    """3c:2c:99:58:9d:02: first octet 0x3c ^ 0x02 = 0x3e."""
    assert ll.eui64_ll_from_mac("3c:2c:99:58:9d:02") == "fe80::3e2c:99ff:fe58:9d02"


def test_eui64_ll_second_example():
    """00:90:fb:61:da:a0: first octet 0x00 ^ 0x02 = 0x02."""
    assert ll.eui64_ll_from_mac("00:90:fb:61:da:a0") == "fe80::290:fbff:fe61:daa0"


def test_eui64_ll_mixed_case_and_dashes():
    """Accepts mixed case and dash separators."""
    assert ll.eui64_ll_from_mac("3C-2C-99-58-9D-02") == "fe80::3e2c:99ff:fe58:9d02"


@pytest.mark.parametrize("mac", [
    "3c:2c:99:58:9d:02",
    "00:90:fb:61:da:a0",
    "AA:BB:CC:DD:EE:FF",
])
def test_eui64_ll_agrees_with_reconcile_kick(mac):
    """flax_observe.ll and flax_reconcile.kick must produce the same EUI-64."""
    assert ll.eui64_ll_from_mac(mac) == _reconcile_eui64(mac)


def test_eui64_ll_bad_mac_raises():
    with pytest.raises(ValueError):
        ll.eui64_ll_from_mac("not:a:mac")


def test_eui64_ll_too_short_raises():
    with pytest.raises(ValueError):
        ll.eui64_ll_from_mac("aa:bb:cc:dd:ee")


def test_eui64_ll_empty_raises():
    with pytest.raises(ValueError):
        ll.eui64_ll_from_mac("")


# ---------------------------------------------------------------------------
# ping6_reachable
# ---------------------------------------------------------------------------

def test_ping6_reachable_runner_returns_true():
    """Injected runner returning True (rc=0 semantics) → True."""
    def runner(addr, timeout):
        return True
    assert ll.ping6_reachable("fe80::1%eth0", runner=runner) is True


def test_ping6_reachable_runner_returns_false():
    """Injected runner returning False (rc=1 semantics) → False."""
    def runner(addr, timeout):
        return False
    assert ll.ping6_reachable("fe80::1%eth0", runner=runner) is False


def test_ping6_reachable_runner_raises_returns_false():
    """Exception in injected runner must not propagate — returns False."""
    def runner(addr, timeout):
        raise RuntimeError("network error")
    assert ll.ping6_reachable("fe80::1%eth0", runner=runner) is False


def test_ping6_reachable_runner_receives_addr_and_timeout():
    """Runner is called with the exact addr and timeout passed in."""
    calls = []

    def runner(addr, timeout):
        calls.append((addr, timeout))
        return True

    ll.ping6_reachable("fe80::dead%eth1.26", timeout=3, runner=runner)
    assert calls == [("fe80::dead%eth1.26", 3)]


# ---------------------------------------------------------------------------
# ll_target
# ---------------------------------------------------------------------------

def test_ll_target_appends_iface():
    result = ll.ll_target("3c:2c:99:58:9d:02", "eth0.26")
    assert result == "fe80::3e2c:99ff:fe58:9d02%eth0.26"


def test_ll_target_second_example():
    result = ll.ll_target("00:90:fb:61:da:a0", "eth1.17")
    assert result == "fe80::290:fbff:fe61:daa0%eth1.17"
