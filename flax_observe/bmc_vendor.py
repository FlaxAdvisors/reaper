"""BMC firmware-vendor classification and the capability table keyed on it.

Pure logic: no subprocess, no ssh, no credential, no network. The callers
(bmc_probe.probe_bmc_kind, state_machine.port_worker_one_iter) do the talking;
this module only decides what a reading MEANS. Same split as bmcfw.thermtrip.

ONE AXIS. The value stored is the firmware vendor and nothing else. Capability
is derived from it through CAPS and is never persisted -- that separation is
the entire point of this module. The taxonomy it replaces mixed axes
("traditional" = a capability, "redfish" = a protocol, "openbmc" = a vendor),
which is why the same Tioga Pass board landed in four different classes
depending on which probe happened to answer.

Spec: docs/superpowers/specs/2026-09-18-bmc-vendor-classification-design.md
"""

AMI_LEGACY = "ami_legacy"   # legacy AMI OEM / MegaRAC: IPMI, no ssh
FACEBOOK = "facebook"       # Facebook OpenBMC: *-util family over ssh, no IPMI-over-LAN
PHOSPHOR = "phosphor"       # Phosphor OpenBMC (incl. our flax-onetree builds)
UNKNOWN = "unknown"

VENDORS = (AMI_LEGACY, FACEBOOK, PHOSPHOR)

# Bumped whenever the meaning of a stored vendor value changes. A cached probe
# stamped with an older version is ignored and re-probed once -- the old
# taxonomy's values CANNOT be translated (a cached "openbmc" may be facebook or
# phosphor; the distinction was never recorded), so migration is by re-probe,
# never by backfill.
TAXONOMY_VERSION = 1

# Transport availability. Tri-state, never a bool, so "answers, but only
# partially" stays distinguishable from "works" and from "do not attempt".
FULL = "full"
PARTIAL = "partial"
NONE = "none"


class Caps(object):
    """What a vendor's firmware can actually serve. Derived, never stored."""

    def __init__(self, ipmi, redfish, ssh, ipmi_cipher=None,
                 redfish_system=None, redfish_manager=None,
                 ssh_family=None, notes=""):
        self.ipmi = ipmi
        self.redfish = redfish
        self.ssh = ssh
        self.ipmi_cipher = ipmi_cipher
        self.redfish_system = redfish_system
        self.redfish_manager = redfish_manager
        self.ssh_family = ssh_family
        self.notes = notes


# Every value below is measured or taken from an existing in-tree comment, not
# assumed. Facebook has no IPMI-over-LAN (scripts/nodepower); Tioga Pass and
# modern OpenBMC reject cipher 3 (collect-bmc-sel-sdr); AMI exposes Manager
# "Self" where Phosphor exposes "bmc" (scripts/bmc-reset-via-redfish.sh).
CAPS = {
    AMI_LEGACY: Caps(
        ipmi=FULL, redfish=PARTIAL, ssh=NONE,
        ipmi_cipher=3,
        redfish_system="Systems/Self", redfish_manager="Self",
        notes="small session table (4-8 slots); a live SOL session is evicted"),
    FACEBOOK: Caps(
        ipmi=NONE, redfish=PARTIAL, ssh=FULL,
        ssh_family="fb-utils",
        notes="no IPMI-over-LAN, UDP 623 closed; driven by /usr/local/bin/*-util"),
    PHOSPHOR: Caps(
        ipmi=FULL, redfish=FULL, ssh=FULL,
        ipmi_cipher=17,
        redfish_system="Systems/system", redfish_manager="bmc",
        ssh_family="dbus",
        notes="rejects cipher 3; Redfish Task never reports Completed"),
}

# Not a vendor we identified -> attempt nothing. Never guess a transport.
_UNKNOWN_CAPS = Caps(ipmi=NONE, redfish=NONE, ssh=NONE,
                     notes="vendor not identified; do not attempt any transport")


def caps_for(vendor):
    """Capabilities for a vendor. An unrecognised value yields the UNKNOWN
    capabilities (all NONE) rather than raising: a caller must degrade to
    'attempt nothing', never to 'attempt everything'."""
    return CAPS.get(vendor, _UNKNOWN_CAPS)


def parse_os_release_id(text):
    """The ID= value from an os-release blob, unquoted. '' when absent.

    Anchored on the ID= line specifically: PRETTY_NAME and CPE_NAME also carry
    the string 'phosphor', so a whole-file substring scan matches them too.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("ID="):
            return line[3:].strip().strip('"').strip("'")
    return ""


def vendor_from_probe(osrelease_text, fb_utils_present):
    """Classify from TWO signals read in the same ssh session.

    fb_utils_present is a tri-state:
      True  -> /usr/local/bin/fruid-util is executable  -> FACEBOOK
      False -> confirmed absent
      None  -> the probe could not be run -> UNKNOWN

    None must NOT fall back to the ID alone. Facebook OpenBMC is itself
    Phosphor-derived and may report ID=openbmc-phosphor, so ID-only would
    classify it PHOSPHOR and route it to IPMI it cannot serve (it has no
    IPMI-over-LAN at all). ghost's getopenbmcfwinfo separates the two
    generations with exactly this *-util test and pointedly NOT with
    os-release -- the same conclusion, reached independently.
    """
    if fb_utils_present is True:
        return FACEBOOK
    if fb_utils_present is None:
        return UNKNOWN
    ident = parse_os_release_id(osrelease_text)
    if not ident or "openbmc" not in ident.lower():
        return UNKNOWN
    if ident.lower() == "openbmc-phosphor":
        return PHOSPHOR
    return FACEBOOK
