#!/usr/bin/env python3
"""tpm_sce.py clear -- schedule a TPM 1.2 clear through the BIOS setup.

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


def find_clear_question(dump):
    for s, e in _blocks(dump):
        blk = dump[s:e]
        name = blk.split(b"\n", 1)[0].split(b"=", 1)[1].strip()
        if name == b"Pending operation" and CLEAR in blk and b"Width\t=01" in blk:
            return blk
    return None


def selected(blk):
    m = re.search(rb"\*\[([0-9A-Fa-f]+)\]", blk)
    return m.group(1).decode().upper() if m else None


def build_clear_script(dump):
    crc = hii(dump)
    if crc not in KNOWN_HII:
        raise Skip("unknown-hii:%s" % crc)
    blk = find_clear_question(dump)
    if blk is None:
        raise Skip("no-clear-question")
    header = dump[:_blocks(dump)[0][0]]
    blk = blk.replace(b"*[", b"[").replace(CLEAR, b"*" + CLEAR)
    return header + blk + b"\n"      # byte-identical to the script proven on et31b4


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


def clear():
    with tempfile.TemporaryDirectory(prefix="tpmsce.") as tmp:
        sce = _sce_bin(tmp)
        before = _dump(sce, tmp, "before.txt")
        script = os.path.join(tmp, "clear.txt")
        with open(script, "wb") as f:
            f.write(build_clear_script(before))
        rc = _run(sce, tmp, "/i", "/s", script)
        if rc != 0:
            raise Fail("sce-import-rc%d" % rc)
        blk = find_clear_question(_dump(sce, tmp, "after.txt"))
        if blk is None or selected(blk) != "05":
            raise Fail("sce-clear-did-not-stick")
        token = re.search(rb"Token\t=([0-9A-Fa-f]+)", blk).group(1).decode()
        return "SCE-QUEUED token=%s hii=%s" % (token, hii(before))


def main(argv):
    if argv[1:] != ["clear"]:
        print("usage: tpm_sce.py clear")
        return 2
    try:
        print(clear())
        return 0
    except Skip as e:
        print("SCE-SKIP reason=%s" % (e if str(e).startswith("sce-") else "sce-%s" % e))
        return 3
    except Fail as e:
        print("SCE-FAIL reason=%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
