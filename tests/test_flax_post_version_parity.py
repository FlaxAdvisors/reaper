"""Post's BMC + BIOS version checks must match the triage environment exactly.

Triage owns the reference semantics in `services/api/{bmcfw,biosfw}/version.py`
(repo `~/git/triage`, developed on bang-gouda). Post is a separate repo and
cannot import them, so these tests pin the same contract on the post side.

Both are EXACT STRING EQUALITY on the stripped string. Neither parses, orders,
nor strips a prefix. Triage's bmcfw/version.py records why the ordering was
removed: production has shipped BOTH `flax-onetree-1.1.1-202608281924` and
`flax-onetree-1.1.1`, and a parse that mapped a missing build stamp to 0 ranked
the stamped build ABOVE the unstamped one -- so a fleet AT the target compared
"newer" than it. The ordering was never used by any caller; it was pure hazard.
"""
import pytest


# --------------------------------------------------------------------------
# BMC -- parity with triage services/api/bmcfw/version.py
# --------------------------------------------------------------------------

def test_bmc_stamped_build_is_not_newer_than_unstamped_target():
    """The exact hazard triage deleted: same semver, one side carries a build
    stamp. Must be 'differs' (flash it), never an ordering verdict."""
    from flax_post.fwd import manifest
    assert manifest.compare("flax-onetree-1.1.1-202608281924",
                            "flax-onetree-1.1.1") == "differs"


def test_bmc_identical_strings_are_same():
    from flax_post.fwd import manifest
    assert manifest.compare("flax-onetree-1.1.1-202608281924",
                            "flax-onetree-1.1.1-202608281924") == "same"


def test_bmc_trailing_newline_is_not_a_difference():
    """The version reader's stdout carries a trailing newline; a stray newline
    must never cost a 13-minute BMC flash and a power cycle."""
    from flax_post.fwd import manifest
    assert manifest.compare("flax-onetree-1.1.1\n", "flax-onetree-1.1.1") == "same"


def test_bmc_prefix_is_not_stripped():
    """No normalisation beyond whitespace: a bare semver is a different string
    from the prefixed one, and post must not silently equate them."""
    from flax_post.fwd import manifest
    assert manifest.compare("1.1.1", "flax-onetree-1.1.1") == "differs"


def test_bmc_compare_never_raises_on_an_unparseable_version():
    """An OEM/AMI board's version is not semver. Triage's compare cannot fail,
    so neither can post's -- callers in bridge.py and flasher.py must not need
    a ValueError guard."""
    from flax_post.fwd import manifest
    assert manifest.compare("not-a-version", "flax-onetree-1.1.1") == "differs"
    assert manifest.compare("AMI-1.10.2-fb", "AMI-1.10.2-fb") == "same"


def test_bmc_needs_update_is_the_negation_of_same():
    from flax_post.fwd import manifest
    assert manifest.needs_update("flax-onetree-1.1.1-202608281924",
                                 "flax-onetree-1.1.1") is True
    assert manifest.needs_update("flax-onetree-1.1.1", "flax-onetree-1.1.1") is False


# --------------------------------------------------------------------------
# BIOS -- parity with triage services/api/biosfw/version.py
# --------------------------------------------------------------------------

def test_bios_literal_null_is_unknown_not_a_version():
    """The BMC reports the literal string 'null' for an unpopulated Version.
    Treating it as a version name classifies an UNREADABLE node as needing an
    update, which under enforce reflashes it on every pass."""
    from flax_post.biosd import classify
    assert classify.classify("null", "TPC_P26F") == "unknown"
    assert classify.classify("NULL", "TPC_P26F") == "unknown"


def test_bios_blank_version_is_unknown():
    from flax_post.biosd import classify
    assert classify.classify("", "TPC_P26F") == "unknown"
    assert classify.classify("   ", "TPC_P26F") == "unknown"
    assert classify.classify(None, "TPC_P26F") == "unknown"


def test_bios_unknown_target_is_unknown_not_needs_update():
    """An absent target must not arm a flash toward nothing."""
    from flax_post.biosd import classify
    assert classify.classify("TPC_P26F", "") == "unknown"
    assert classify.classify("TPC_P26F", None) == "unknown"
    assert classify.classify("TPC_P26F", "null") == "unknown"


def test_bios_surrounding_whitespace_is_not_a_difference():
    """dmidecode capture carries a trailing newline; without a strip the node
    sits at needs_update forever and reflashes every pass under enforce."""
    from flax_post.biosd import classify
    assert classify.classify("TPC_P26F\n", "TPC_P26F") == "up_to_date"
    assert classify.classify(" TPC_P26F ", "TPC_P26F") == "up_to_date"


def test_bios_comparison_stays_case_sensitive():
    """BIOS versions are opaque vendor strings; case is meaningful."""
    from flax_post.biosd import classify
    assert classify.classify("tpc_p26f", "TPC_P26F") == "needs_update"


def test_bios_differing_known_versions_need_update():
    from flax_post.biosd import classify
    assert classify.classify("TPC_P26C", "TPC_P26F") == "needs_update"
