#!/usr/bin/env python3
"""tpm_state.py -- is this node's TPM 2.0 owned? Raw TPM2_GetCapability over
/dev/tpm0, because the live ISO's tpm2-tools ship without the device TCTI
("Could not load tcti", 2026-10-26).

Prints one line of key=value pairs, e.g.
  owned=1 perm=0x00000007 ownerAuthSet=1 endorsementAuthSet=1 lockoutAuthSet=1 disableClear=0 persistent=2
owned=1 when any of the three hierarchy passwords is set or a persistent key
exists -- what a previous OS (Windows/BitLocker) leaves behind. A cleared TPM
reads perm=0x00000000 persistent=0. Platform/vendor NV indices (EK certs)
survive a clear and are not counted. Exit 2 when the TPM cannot be read.

Enabled (TiogaPass, operator 2026-10-29): TPM_PT_STARTUP_CLEAR adds
  ... startup=0x80000006 shEnable=1 ehEnable=1 phEnable=0
A TPM 2.0 the BIOS has turned off ("TPM disabled", PPI op 2) answers with the
storage and endorsement hierarchies disabled -- shEnable=0 / ehEnable=0 is the
2.0 counterpart of 1.2's enabled=0. phEnable=0 is normal: the BIOS shuts the
platform hierarchy before the OS boots. Left out when that read fails.

TPM 1.2 (Leopards, operator 2026-10-27) does not speak the TPM2 command set
(TPM_rc 0xa, bad ordinal). When the kernel reports tpm_version_major 1, the
answer comes from its TPM 1.2 sysfs attributes instead:
  owned=1 tpm=1.2 enabled=1 active=1
(<sysfs>/owned, or <sysfs>/device/owned on older kernels).
"""
import os
import struct
import sys

DEV = os.environ.get("TPM_DEV", "/dev/tpm0")
SYSFS = os.environ.get("TPM_SYSFS", "/sys/class/tpm/tpm0")
BITS = {0: "ownerAuthSet", 1: "endorsementAuthSet", 2: "lockoutAuthSet", 8: "disableClear"}
STARTUP_BITS = {1: "shEnable", 2: "ehEnable", 0: "phEnable"}


def get_capability(f, cap, prop, count):
    body = struct.pack(">III", cap, prop, count)
    f.write(struct.pack(">HII", 0x8001, 10 + len(body), 0x0000017A) + body)
    r = f.read(4096)
    rc = struct.unpack(">I", r[6:10])[0]
    if rc:
        raise IOError("TPM rc=0x%x" % rc)
    return r[10:]


def _read(name):
    for d in (SYSFS, os.path.join(SYSFS, "device")):
        try:
            with open(os.path.join(d, name)) as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def tpm12_state():
    """'owned=... tpm=1.2 enabled=... active=...', or None when unreadable."""
    owned = _read("owned")
    if owned not in ("0", "1"):
        return None
    return "owned=%s tpm=1.2 enabled=%s active=%s" % (
        owned, _read("enabled") or "?", _read("active") or "?")


def main():
    if _read("tpm_version_major") == "1":
        line = tpm12_state()
        if line is None:
            print("owned=? tpm=1.2 error=no-owned-attribute")
            return 2
        print(line)
        return 0
    try:
        with open(DEV, "r+b", buffering=0) as f:
            r = get_capability(f, 6, 0x200, 1)               # TPM_PT_PERMANENT
            perm = struct.unpack(">I", r[13:17])[0]
            r = get_capability(f, 1, 0x81000000, 64)          # persistent handles
            persistent = struct.unpack(">I", r[5:9])[0]
            try:
                r = get_capability(f, 6, 0x201, 1)           # TPM_PT_STARTUP_CLEAR
                prop, startup = struct.unpack(">II", r[9:17])
                if prop != 0x201:                             # TPM answered the next property
                    startup = None
            except (IOError, struct.error):
                startup = None
    except (OSError, IOError, struct.error) as e:
        print("owned=? error=%s" % str(e).replace(" ", "_"))
        return 2
    owned = int(bool(perm & 0x7) or persistent > 0)
    line = "owned=%d perm=0x%08x %s persistent=%d" % (
        owned, perm, " ".join("%s=%d" % (n, (perm >> b) & 1) for b, n in BITS.items()), persistent)
    if startup is not None:
        line += " startup=0x%08x %s" % (startup, " ".join(
            "%s=%d" % (n, (startup >> b) & 1) for b, n in STARTUP_BITS.items()))
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
