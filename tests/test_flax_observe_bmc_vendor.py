"""Tests for flax_observe.bmc_vendor.

The taxonomy is firmware-VENDOR only; capability is a lookup keyed on it and is
never stored. See docs/superpowers/specs/2026-09-18-bmc-vendor-classification-design.md
"""
import pytest

from flax_observe import bmc_vendor as bv


# Captured verbatim from eindhoven et6b1, 2026-09-18.
PHOSPHOR_OSRELEASE = """\
ID=openbmc-phosphor
NAME="Phosphor OpenBMC (Phosphor OpenBMC Project Reference Distro)"
VERSION="flax-onetree-1.1.2"
VERSION_ID=flax-onetree-1.1.2-202609091341
VERSION_CODENAME="scarthgap"
PRETTY_NAME="Phosphor OpenBMC (Phosphor OpenBMC Project Reference Distro) flax-onetree-1.1.2"
CPE_NAME="cpe:/o:openembedded:openbmc-phosphor:flax-onetree-1.1.2-202609091341"
OPENBMC_TARGET_MACHINE="tiogapass"
"""

# Captured verbatim from braintree et8b1, 2026-09-18 -- an OLDER build of our
# own image. Same ID, different VERSION_ID: the rule must key on identity, not
# on build version.
PHOSPHOR_OSRELEASE_OLD = """\
ID=openbmc-phosphor
NAME="Phosphor OpenBMC (Phosphor OpenBMC Project Reference Distro)"
VERSION="flax-onetree-1.0.9"
VERSION_ID=flax-onetree-1.0.9-202607032146
"""

# No live Facebook board was reachable on 2026-09-18 (braintree's BryceCanyon
# and Wedge both unreachable), so this is a CONSTRUCTED fixture, deliberately
# using the same ID as Phosphor -- the worst case the two-signal rule exists to
# survive. Replace with captured output if an FB board returns to service.
FACEBOOK_OSRELEASE_WORST_CASE = """\
ID=openbmc-phosphor
NAME="OpenBMC (Phosphor OpenBMC Project Reference Distro)"
VERSION_ID=fbobmc-1.0
"""


def test_phosphor_is_our_image_with_no_facebook_utils():
    assert bv.vendor_from_probe(PHOSPHOR_OSRELEASE,
                                fb_utils_present=False) == bv.PHOSPHOR


def test_phosphor_rule_keys_on_identity_not_build_version():
    assert bv.vendor_from_probe(PHOSPHOR_OSRELEASE_OLD,
                                fb_utils_present=False) == bv.PHOSPHOR


def test_facebook_utils_win_even_when_the_id_says_phosphor():
    """The whole reason for the second signal: Facebook OpenBMC is itself
    Phosphor-derived and may report the same ID. The *-util family is what
    actually distinguishes the generations (ghost's getopenbmcfwinfo uses
    exactly this test, and pointedly NOT os-release)."""
    assert bv.vendor_from_probe(FACEBOOK_OSRELEASE_WORST_CASE,
                                fb_utils_present=True) == bv.FACEBOOK


def test_unknown_when_the_fb_utils_probe_could_not_run():
    """None means 'could not determine', which is NOT False. Falling back to
    the ID alone would classify a Facebook board as phosphor and route it to
    IPMI it does not serve."""
    assert bv.vendor_from_probe(PHOSPHOR_OSRELEASE,
                                fb_utils_present=None) == bv.UNKNOWN


def test_non_openbmc_os_release_is_unknown_not_a_guess():
    assert bv.vendor_from_probe("ID=ubuntu\nNAME=Ubuntu\n",
                                fb_utils_present=False) == bv.UNKNOWN


def test_empty_os_release_is_unknown():
    assert bv.vendor_from_probe("", fb_utils_present=False) == bv.UNKNOWN
    assert bv.vendor_from_probe(None, fb_utils_present=False) == bv.UNKNOWN


def test_parse_id_ignores_pretty_name_and_cpe_name():
    """PRETTY_NAME and CPE_NAME also contain 'phosphor'; a substring scan over
    the whole file would match them. Anchor on the ID= line only."""
    assert bv.parse_os_release_id(PHOSPHOR_OSRELEASE) == "openbmc-phosphor"


def test_parse_id_strips_quotes_and_whitespace():
    assert bv.parse_os_release_id('ID="openbmc-phosphor"  \n') == "openbmc-phosphor"


def test_phosphor_speaks_ipmi_and_redfish_and_ssh():
    c = bv.caps_for(bv.PHOSPHOR)
    assert (c.ipmi, c.redfish, c.ssh) == (bv.FULL, bv.FULL, bv.FULL)
    assert c.ipmi_cipher == 17


def test_facebook_has_no_ipmi_over_lan():
    """UDP 623 is closed on these boards -- driven by *-util over ssh."""
    c = bv.caps_for(bv.FACEBOOK)
    assert c.ipmi == bv.NONE
    assert c.ssh == bv.FULL


def test_ami_legacy_speaks_ipmi_but_not_ssh():
    c = bv.caps_for(bv.AMI_LEGACY)
    assert c.ipmi == bv.FULL
    assert c.ssh == bv.NONE
    assert c.ipmi_cipher == 3


def test_unknown_vendor_attempts_nothing():
    """A vendor we could not identify must not be probed over any transport --
    never guess, never default to a vendor."""
    c = bv.caps_for(bv.UNKNOWN)
    assert (c.ipmi, c.redfish, c.ssh) == (bv.NONE, bv.NONE, bv.NONE)


def test_caps_for_an_unrecognised_string_is_the_unknown_caps():
    c = bv.caps_for("wat")
    assert (c.ipmi, c.redfish, c.ssh) == (bv.NONE, bv.NONE, bv.NONE)


def test_taxonomy_version_is_an_int_for_the_cache_stamp():
    assert isinstance(bv.TAXONOMY_VERSION, int)
    assert bv.TAXONOMY_VERSION >= 1


_CREDS = [{"bmcuser": "USERID", "bmcpass": "a"}, {"bmcuser": "root", "bmcpass": "b"},
          {"bmcuser": "admin", "bmcpass": "c"}]


def test_ipmi_login_pairs_cipher_3_with_entry_0_and_17_with_entry_1():
    from flax_observe import bmc_vendor as bv
    assert bv.ipmi_login(3, _CREDS) == ("USERID", "a")
    assert bv.ipmi_login(17, _CREDS) == ("root", "b")


def test_ipmi_login_unknown_cipher_is_none():
    from flax_observe import bmc_vendor as bv
    assert bv.ipmi_login(None, _CREDS) is None
    assert bv.ipmi_login(1, _CREDS) is None


def test_ipmi_login_reordered_file_is_none_and_logs_once(caplog):
    from flax_observe import bmc_vendor as bv
    bv._ORDER_WARNED = False
    swapped = [_CREDS[1], _CREDS[0]]
    with caplog.at_level("ERROR"):
        assert bv.ipmi_login(17, swapped) is None
        assert bv.ipmi_login(3, swapped) is None
    assert sum("ipmi_login_order_mismatch" in r.message for r in caplog.records) == 1
    assert bv.login_order_ok(swapped) is False
    assert bv.login_order_ok(_CREDS) is True


def test_ipmi_login_short_file_is_none():
    from flax_observe import bmc_vendor as bv
    assert bv.ipmi_login(3, _CREDS[:1]) is None
    assert bv.ipmi_login(17, []) is None
