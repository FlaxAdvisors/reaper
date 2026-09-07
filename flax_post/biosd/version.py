"""BIOS version comparison.

PARITY: this mirrors triage `services/api/biosfw/version.py`. Post and triage
are separate repos and cannot share a package, so the semantics are duplicated
here deliberately. Change both or neither.

Unlike the BMC's flax-onetree versions, BIOS versions are OPAQUE VENDOR STRINGS
('TPC_P26C', 'TPC_P26F', 'F08_3A24'). There is no ordering to derive, so this
is exact equality on the stripped string -- never a numeric parse.

The BMC reports the literal string "null" for an unpopulated bios_active
Version, which must be treated as unknown, not as a version named "null".
"""
_UNKNOWN = ("", "null")


def is_unknown(v):
    """True when v carries no usable version (None, blank, or the BMC's 'null')."""
    if v is None:
        return True
    return v.strip().lower() in _UNKNOWN


def same(reported, target):
    """Exact (case-sensitive) equality. Unknown on either side is never 'same'."""
    if is_unknown(reported) or is_unknown(target):
        return False
    return reported.strip() == target.strip()


def needs_update(reported, target):
    """True iff both sides are known AND differ.

    Unknown must NOT imply 'needs update' -- an unreadable version would
    otherwise flash every node on every pass."""
    if is_unknown(reported) or is_unknown(target):
        return False
    return not same(reported, target)
