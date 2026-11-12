from flax_post.nicd import driver

_QUERY_OUT = """===CARD 5e:00.0===
Image type:            FS3
FW Version:            14.20.1202
PSID:                  FB_2450111034
Security Attributes:   N/A
===UEFI 5e:00.0===
         EXP_ROM_UEFI_x86_ENABLE             False(0)
===CARD d8:00.0===
FW Version:            14.32.1010
PSID:                  MT_2450112034
Security Attributes:   secure-fw
===UEFI d8:00.0===
         EXP_ROM_UEFI_x86_ENABLE             True(1)
"""


def test_parse_cards_two_devices():
    cards = driver.parse_cards(_QUERY_OUT)
    assert [c["pci"] for c in cards] == ["5e:00.0", "d8:00.0"]
    a, b = cards
    assert a == {"pci": "5e:00.0", "current": "14.20.1202", "psid": "FB_2450111034", "secure": False, "uefi": False}
    assert b == {"pci": "d8:00.0", "current": "14.32.1010", "psid": "MT_2450112034", "secure": True, "uefi": True}


def test_parse_cards_empty():
    assert driver.parse_cards("") == []
    assert driver.parse_cards("no mellanox devices") == []


def test_query_script_enumerates_and_delimits():
    s = driver.QUERY_SCRIPT
    assert "lspci -d 15b3:" in s and "00.0" in s
    assert "===CARD" in s and "===UEFI" in s
    assert "mstflint -d" in s and "mstconfig -d" in s and "EXP_ROM_UEFI_x86_ENABLE" in s


def test_flash_script_burns_with_psid_change_and_resets():
    entry = {"dir": "MCX4411A-ACQN", "bin": "fw-x", "target_version": "14.32.1010", "target_psid": "MT_2450112034"}
    s = driver.flash_script(entry, "5e:00.0", "http://bang/export/share")
    assert "http://bang/export/share/mellanox/MCX4411A-ACQN/fw-x.zip" in s and "unzip" in s
    assert "mstflint -d 5e:00.0 -y -i" in s and "--allow_psid_change burn" in s
    assert "mstfwreset -d 5e:00.0 -y reset" in s


def test_uefi_script_sets_and_resets():
    s = driver.uefi_script("5e:00.0")
    assert "mstconfig -d 5e:00.0 -y set EXP_ROM_UEFI_x86_ENABLE=true" in s
    assert "mstfwreset -d 5e:00.0 -y reset" in s


def test_card_query_script_single_device():
    s = driver.card_query_script("5e:00.0")
    assert "===CARD 5e:00.0===" in s and "mstflint -d 5e:00.0 query" in s


def test_parse_cards_uefi_absent_is_none():
    # A card whose mstconfig output doesn't expose EXP_ROM_UEFI_x86_ENABLE
    # (older FW / some models) -> uefi None (not-applicable), not False.
    out = ("===CARD 5e:00.0===\nFW Version:  14.20.1202\nPSID:  FB_2450111034\n"
           "Security Attributes:  N/A\n===UEFI 5e:00.0===\n LEGACY_BOOT_PROTOCOL PXE(1)\n")
    cards = driver.parse_cards(out)
    assert cards[0]["uefi"] is None

def test_query_scripts_tolerate_grep_no_match():
    # grep returns 1 when EXP_ROM_UEFI_x86_ENABLE is absent; `|| true` must keep
    # the script's exit code 0 so a reachable host isn't mislabeled unreachable.
    assert "|| true" in driver.QUERY_SCRIPT
    assert "|| true" in driver.card_query_script("5e:00.0")


def test_flash_and_uefi_scripts_detach_the_reset():
    # mstfwreset severs the SSH link it rides -> must be backgrounded (nohup+&)
    # so the burn/uefi SSH returns instead of hanging on the dead connection.
    fs = driver.flash_script({"dir": "D", "bin": "b"}, "5e:00.0", "http://bang/export/share")
    assert "nohup mstfwreset -d 5e:00.0 -y reset" in fs and fs.count("&\n") >= 1
    us = driver.uefi_script("5e:00.0")
    assert "nohup mstfwreset -d 5e:00.0 -y reset" in us and us.count("&\n") >= 1


