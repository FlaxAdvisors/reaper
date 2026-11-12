"""Smoke tests for the flax-switch-sense package."""


def test_package_imports():
    """Confirm the package can be imported and exposes __version__."""
    import flax_switch_sense
    assert flax_switch_sense.__version__ == "0.1.0"


def test_normalize_mac_strips_separators():
    from flax_switch_sense.mac import normalize_mac
    assert normalize_mac("98:03:9b:a6:fc:24") == "98:03:9b:a6:fc:24"
    assert normalize_mac("98-03-9b-A6-FC-24") == "98:03:9b:a6:fc:24"
    assert normalize_mac("9803.9ba6.fc24") == "98:03:9b:a6:fc:24"


def test_normalize_mac_rejects_short_input():
    import pytest
    from flax_switch_sense.mac import normalize_mac
    with pytest.raises(ValueError):
        normalize_mac("98:03:9b:a6")


def test_mac_int_roundtrip():
    from flax_switch_sense.mac import normalize_mac, mac_to_int, int_to_mac
    mac = "98:03:9b:a6:fc:24"
    assert int_to_mac(mac_to_int(mac)) == mac


def test_int_to_mac_masks_overflow():
    from flax_switch_sense.mac import int_to_mac
    # Should mask to 48 bits, not raise
    assert int_to_mac(2 ** 49 + 0x9803_9ba6_fc24) == "98:03:9b:a6:fc:24"


def test_is_paired_nic_matches_stride():
    from flax_switch_sense.mac import is_paired_nic_of
    bmc = "98:03:9b:a6:fc:24"
    # bmc - 2 (k=1, paired-host)
    assert is_paired_nic_of("98:03:9b:a6:fc:22", bmc)
    # bmc - 4 (k=2, sameport blade 2)
    assert is_paired_nic_of("98:03:9b:a6:fc:20", bmc)
    # bmc - 8 (k=4, sameport blade 4)
    assert is_paired_nic_of("98:03:9b:a6:fc:1c", bmc)
    # bmc - 1 (off-pattern)
    assert not is_paired_nic_of("98:03:9b:a6:fc:23", bmc)
    # unrelated MAC
    assert not is_paired_nic_of("00:11:22:33:44:55", bmc)
