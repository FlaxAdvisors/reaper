"""BMC firmware version comparison -- exact string equality, no parsing.

PARITY: this mirrors triage `services/api/bmcfw/version.py`. Post and triage are
separate repos and cannot share a package, so the semantics are duplicated here
deliberately. Change both or neither.

A BMC version string is an opaque build identifier, not something to order.
Production builds have shipped in BOTH forms:

    flax-onetree-1.1.1-202608281924    semver + build minute
    flax-onetree-1.1.1                 semver alone

The previous post implementation parsed these into a numeric tuple and mapped a
MISSING build stamp to 0, so the same semver sorted differently depending on
whether a stamp was present -- and a stamped build always ranked ABOVE an
unstamped one. In post that surfaced three ways, all wrong:

  * the probe path (`flasher.probe_one`) mapped anything not "older" to
    up_to_date, so a node needing an update reported as current;
  * the flash path faulted it as "refusing downgrade";
  * the post-flash verify required "same", so a stamped return against an
    unstamped target never confirmed and the claim was held.

It also STRIPPED the `flax-onetree-` prefix, so a BMC reporting a bare `1.1.1`
read as at-target against `flax-onetree-1.1.1`. And it RAISED ValueError on any
non-semver string, which every caller had to guard.

The ordering was never used: callers only ever asked `== "same"`. So it was pure
hazard, and it is gone. The rule is now simply: if the strings differ, the image
differs, so flash.
"""


def compare(reported, target):
    """Return 'same' if the two version strings are identical, else 'differs'.

    Surrounding whitespace is not a difference -- the version reader's stdout
    carries a trailing newline, and a stray newline must never cost a
    13-minute BMC flash and a power cycle. Nothing else is normalised: no
    prefix stripping, no ordering, no parsing, and therefore no version this
    function can fail to understand.
    """
    return "same" if (reported or "").strip() == (target or "").strip() else "differs"


def needs_update(reported, target):
    """True iff the reported version is not exactly the target."""
    return compare(reported, target) != "same"
