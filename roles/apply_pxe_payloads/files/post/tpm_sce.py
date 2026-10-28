#!/usr/bin/env python3
"""tpm_sce.py clear|enable -- TPM 1.2 clear / re-enable through the BIOS setup.

For Quanta Leopards (TPM 1.2), whose BIOS requires a console confirmation for
every PPI clear op and implements no NoPPIClear flag. Their AMI setup has a
"Pending operation" question ([05]TPM Clear) under Trusted Computing; set it in
setup NVRAM with AMI SCE (SCELNX_64 /i) and the BIOS clears the TPM on the next
boot with no prompt (et31b4, F06_3B17, 2026-10-27, SOL-verified).

Steps: fresh `/o` dump -> require a known HIICrc32 -> find the question BY NAME
(token numbers are not unique: Token 94 is also "Port 4") -> import a one-question
script that starts with the dump's own header bytes (SCE answers rc 13 "Script
file error" to any other header) -> dump again and require [05] selected.

SCELNX_64 is not bundled in post.tgz (docs/post-platform-firmware-tooling.md):
it is fetched from the share's tpm_enabler.v4.tgz, or TPM_SCE_BIN names one.

`enable` (et29b4 2026-10-28): after the clear the BIOS syncs its setup to
"TPM State = Disabled" and re-applies that on every POST, so PPI op 6 answers
"6 0: Success" and changes nothing. Writing TPM State = Enabled the same way
and rebooting brings the TPM back enabled + active.

Prints one line; exit 0 SCE-QUEUED, 3 skip (nothing written), 1 failure.
"""
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

SHARE_TGZ = os.environ.get(
    "TPM_SCE_TGZ_URL",
    "http://bang/share/SXC-Lab_OCP-Updates/Linux_Flash_Utilities/tpm_enabler.v4.tgz")
# BIOS builds whose "Pending operation" [05] is known to be the TPM 1.2 clear.
# A new build is skipped (reported) until someone checks its dump and adds it.
KNOWN_HII = {
    "492F03E4": "Quanta Leopard ORv2-DDR4 F06_3B17 + F06_3B22",
}
CLEAR = b"[05]TPM Clear"
ENABLED = b"[01]Enabled"


class Skip(Exception):
    pass


class Fail(Exception):
    pass


def _blocks(dump):
    """(start, end) byte spans of each 'Setup Question' block."""
    starts = [m.start() for m in re.finditer(rb"^Setup Question", dump, re.M)]
    spans = []
    for s in starts:
        e = dump.find(b"\n\n", s)
        spans.append((s, len(dump) if e < 0 else e + 1))
    return spans


def hii(dump):
    m = re.search(rb"^HIICrc32= *([0-9A-Fa-f]+)", dump, re.M)
    return m.group(1).decode().upper() if m else None


def _find(dump, name, option):
    for s, e in _blocks(dump):
        blk = dump[s:e]
        if (blk.split(b"\n", 1)[0].split(b"=", 1)[1].strip() == name
                and option in blk.split(b"Options", 1)[-1] and b"Width\t=01" in blk):
            return blk
    return None


def find_clear_question(dump):
    return _find(dump, b"Pending operation", CLEAR)


def find_enable_question(dump):
    return _find(dump, b"TPM State", ENABLED)


def _select(blk, option):
    """Move the "*" onto `option`, in the Options lines ONLY: the BIOS Default
    line can read the same "[01]Enabled", and SCE rejects a script whose
    BIOS Default carries a "*" (rc 13, "Unexpected BIOS Default")."""
    head, opts = blk.split(b"Options", 1)
    return head + b"Options" + opts.replace(b"*[", b"[").replace(option, b"*" + option)


def selected(blk):
    m = re.search(rb"\*\[([0-9A-Fa-f]+)\]", blk)
    return m.group(1).decode().upper() if m else None


def _script(dump, blk, option):
    return dump[:_blocks(dump)[0][0]] + _select(blk, option) + b"\n"   # byte-identical to et31b4's


def build_clear_script(dump):
    crc = hii(dump)
    if crc not in KNOWN_HII:
        raise Skip("unknown-hii:%s" % crc)
    blk = find_clear_question(dump)
    if blk is None:
        raise Skip("no-clear-question")
    return _script(dump, blk, CLEAR)


def build_enable_script(dump):
    crc = hii(dump)
    if crc not in KNOWN_HII:
        raise Skip("unknown-hii:%s" % crc)
    blk = find_enable_question(dump)
    if blk is None:
        raise Skip("no-tpm-state-question")
    if selected(blk) == "01":
        # Rewriting the value it already holds changes nothing at POST.
        raise Skip("setup-already-enabled")
    return _script(dump, blk, ENABLED)


def _sce_bin(tmp):
    if os.environ.get("TPM_SCE_BIN"):
        return os.environ["TPM_SCE_BIN"]
    tgz = os.path.join(tmp, "tpm_enabler.tgz")
    try:
        urllib.request.urlretrieve(SHARE_TGZ, tgz)
        with tarfile.open(tgz) as t:
            t.extract("SCELNX_64", tmp)
    except Exception as e:
        raise Skip("sce-fetch:%s" % type(e).__name__)
    path = os.path.join(tmp, "SCELNX_64")
    os.chmod(path, 0o755)
    return path


def _run(sce, tmp, *args):
    p = subprocess.run([sce] + list(args), cwd=tmp, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=300)
    return p.returncode


def _dump(sce, tmp, name):
    out = os.path.join(tmp, name)
    rc = _run(sce, tmp, "/o", "/s", out)
    if rc != 0 or not os.path.exists(out):
        raise Fail("sce-export-rc%d" % rc)
    with open(out, "rb") as f:
        return f.read()


ACTIONS = {
    # command: (build the script, find the question again, value it must hold)
    "clear": (build_clear_script, find_clear_question, "05"),
    "enable": (build_enable_script, find_enable_question, "01"),
}


def queue(action):
    build, find, want = ACTIONS[action]
    with tempfile.TemporaryDirectory(prefix="tpmsce.") as tmp:
        sce = _sce_bin(tmp)
        before = _dump(sce, tmp, "before.txt")
        script = os.path.join(tmp, action + ".txt")
        with open(script, "wb") as f:
            f.write(build(before))
        rc = _run(sce, tmp, "/i", "/s", script)
        if rc != 0:
            raise Fail("sce-import-rc%d" % rc)
        blk = find(_dump(sce, tmp, "after.txt"))
        if blk is None or selected(blk) != want:
            raise Fail("sce-%s-did-not-stick" % action)
        token = re.search(rb"Token\t=([0-9A-Fa-f]+)", blk).group(1).decode()
        return "SCE-QUEUED token=%s hii=%s" % (token, hii(before))


def main(argv):
    if len(argv) != 2 or argv[1] not in ACTIONS:
        print("usage: tpm_sce.py clear|enable")
        return 2
    try:
        print(queue(argv[1]))
        return 0
    except Skip as e:
        print("SCE-SKIP reason=%s" % (e if str(e).startswith("sce-") else "sce-%s" % e))
        return 3
    except Fail as e:
        print("SCE-FAIL reason=%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
