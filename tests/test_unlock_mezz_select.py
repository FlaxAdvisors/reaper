"""Which Mellanox card the unlock station is allowed to flash.

Selection is by EXCLUSION, and as of 2026-09-21 `PROTECT_PCI` is the ONLY
thing that excludes. A card is protected as a whole -- both functions of a
dual-port card share one flash -- so protection is matched at bus:device
granularity, never per-function.

The station used to also exclude any card holding an IPv4 address or backing
the default route, on the theory that burning the card carrying the operator's
ssh session would kill the run mid-flash. That is REMOVED by operator decision
(2026-09-21): we routinely flash NICs on PXE-live-booted machines and the link
comes back after the FW update and reset, so the caution bought nothing and
cost real coverage. It was actively harmful -- a failed-unlock card that had
been given an address was silently skipped, which is exactly the card that most
needs another pass.

An addressed card is still worth REPORTING (flashing the card under your own
session is worth knowing about); it is just not a reason to refuse.
"""
import importlib.util
import os

_REAPER = os.path.join(os.path.dirname(__file__), os.pardir)
_PATH = os.path.join(_REAPER, "scripts", "unlock-with-ubuntu", "payload",
                     "mezz_select.py")
_spec = importlib.util.spec_from_file_location("mezz_select", _PATH)
mezz_select = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mezz_select)


# The development shape: uplink is a dual-port card at 19:00, addressed on its
# SECOND function (enp25s0f1np1, 172.17.0.108). The mezz at 06:00 may or may
# not hold an address -- a jumpered card passes no traffic, but a card that has
# already been unlocked and rebooted will have one.
DEV_DEVICES = ["06:00.0", "19:00.0", "19:00.1"]
DEV_IFACES = [
    {"name": "enp25s0f1np1", "pci": "19:00.1", "ipv4": True, "default_route": True},
    {"name": "enp6s0np0", "pci": "06:00.0", "ipv4": False, "default_route": False},
]


def test_an_addressed_card_is_still_a_target():
    """The reversal. An IPv4 address is no longer a veto -- a failed-unlock
    card that came back up with a lease must not be skipped."""
    got = mezz_select.select(DEV_DEVICES, DEV_IFACES)
    assert "19:00.0" in got["targets"], got


def test_the_default_route_card_is_still_a_target():
    """Same for the card carrying the default route."""
    ifaces = [{"name": "a", "pci": "06:00.0", "ipv4": True,
               "default_route": True}]
    assert mezz_select.select(["06:00.0"], ifaces)["targets"] == ["06:00.0"]


def test_protect_pci_is_the_only_exclusion():
    """The operator's hard override, now the sole gate. Accepted as a bare
    bus:device or a full BDF."""
    got = mezz_select.select(DEV_DEVICES, DEV_IFACES, protect=["19:00"])
    assert got["targets"] == ["06:00.0"], got
    got = mezz_select.select(DEV_DEVICES, DEV_IFACES, protect=["19:00.1"])
    assert got["targets"] == ["06:00.0"], got


def test_protection_covers_the_whole_card_not_just_the_named_function():
    """PROTECT_PCI names function 1; the flash happens on function 0. Matching
    per-function would leave 19:00.0 selectable -- the same catastrophe by a
    narrower path."""
    got = mezz_select.select(DEV_DEVICES, DEV_IFACES, protect=["19:00.1"])
    assert "19:00.0" not in got["targets"]
    assert "19:00" in got["protected"]


def test_only_function_zero_is_offered_as_a_target():
    """A dual-port card is ONE flash. mstflint is addressed at function 0;
    handing back 06:00.1 as well would burn the same card twice."""
    devices = ["06:00.0", "06:00.1"]
    assert mezz_select.select(devices, [])["targets"] == ["06:00.0"]


def test_nothing_is_excluded_when_no_protect_pci_is_set():
    """A deployed blade has an empty PROTECT_PCI, so every card is fair game --
    including ones holding addresses."""
    ifaces = [
        {"name": "a", "pci": "19:00.1", "ipv4": True, "default_route": True},
        {"name": "b", "pci": "06:00.0", "ipv4": True, "default_route": False},
    ]
    got = mezz_select.select(DEV_DEVICES, ifaces)
    assert got["targets"] == ["06:00.0", "19:00.0"], got
    assert got["protected"] == [], got


def test_addressed_cards_are_reported_even_though_they_are_not_excluded():
    """Flashing the card under your own ssh session is worth knowing about in
    the log, even though it no longer stops the run."""
    got = mezz_select.select(DEV_DEVICES, DEV_IFACES)
    assert got["addressed"] == ["19:00"], got
