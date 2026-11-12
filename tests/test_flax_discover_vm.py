from flax_discover.vm import normalise_mac, is_vm_mac


def test_normalise_mac_accepts_dashes_and_dots():
    assert normalise_mac("0C-42-A1-00-00-01") == "0c:42:a1:00:00:01"


def test_is_vm_mac_virtualbox_and_kvm_ouis():
    assert is_vm_mac("08:00:27:11:22:33") is True
    assert is_vm_mac("52:54:00:11:22:33") is True


def test_is_vm_mac_laa_bit():
    assert is_vm_mac("02:00:00:00:00:01") is True   # LAA set
    assert is_vm_mac("0c:42:a1:00:00:01") is False  # UAA, real NIC
