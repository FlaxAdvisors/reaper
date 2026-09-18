#!/usr/bin/env python3
# scripts/unlock-with-ubuntu/fwmap.py
"""Extract the PSID->image map from update_mellanox.sh.

The unlock-with-ubuntu bundle needs the same PSID->image intent that burns
cards in the post lane. It reads that intent OUT of the shell source rather
than carrying its own copy -- the repo already has three hand-synced copies
(FWMAP, gen-nic-fw-manifest's INTENT, nic-firmware-versions.json) and the
generator's own docstring warns about them drifting.

The shell writes bin names as "...-rel-${FWREL}-<OPN>-${FWSFX}"; the bundle
needs the literal filename to locate the image on the mellanox share, so the
two vars are expanded here.
"""
import re


class FwmapError(Exception):
    """The map could not be read with confidence.

    Always fatal to a build: a partial or empty map yields a bundle that
    installs cleanly and then flashes nothing (or the wrong image), with no
    signal at the rack -- the station's only output is a 'done' blink.
    """


_FWREL = re.compile(r"^FWREL=(\S+)\s*$", re.MULTILINE)
_FWSFX = re.compile(r"^FWSFX=(\S+)\s*$", re.MULTILINE)
_OPEN = "declare -A FWMAP=("
# [MT_2420110034]="MT_2420110034|fw-ConnectX4Lx-rel-${FWREL}-MCX4121A-ACA_Ax-${FWSFX}"
_ENTRY = re.compile(r'^\s*\[([A-Za-z0-9_]+)\]="([^"|]+)\|([^"]+)"', re.MULTILINE)


def _block(text):
    """The text between `declare -A FWMAP=(` and its closing paren."""
    start = text.find(_OPEN)
    if start < 0:
        raise FwmapError("no %r in the source -- has update_mellanox.sh been "
                         "restructured?" % _OPEN)
    start += len(_OPEN)
    end = text.find("\n)", start)
    if end < 0:
        raise FwmapError("FWMAP block is never closed by a lone ')'")
    return text[start:end]


def _var(pattern, name, text):
    m = pattern.search(text)
    if not m:
        raise FwmapError("no %s= assignment; bin names cannot be expanded" % name)
    return m.group(1)


def parse(text):
    """update_mellanox.sh source -> {psid: {"dir": ..., "bin": ...}}."""
    rel = _var(_FWREL, "FWREL", text)
    sfx = _var(_FWSFX, "FWSFX", text)
    out = {}
    for psid, fwdir, binname in _ENTRY.findall(_block(text)):
        binname = binname.replace("${FWREL}", rel).replace("${FWSFX}", sfx)
        if "$" in binname:
            raise FwmapError(
                "%s: unexpanded shell variable left in %r -- a new ${VAR} was "
                "added to the bin template and this parser does not know it"
                % (psid, binname))
        out[psid] = {"dir": fwdir, "bin": binname}
    if not out:
        raise FwmapError("FWMAP block parsed to zero entries")
    return out


def to_tsv(fwmap):
    """{psid: {dir, bin}} -> 'psid\\tdir\\tbin' rows, sorted for reproducibility."""
    return "".join("%s\t%s\t%s\n" % (p, fwmap[p]["dir"], fwmap[p]["bin"])
                   for p in sorted(fwmap))


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("usage: fwmap.py <path-to-update_mellanox.sh>   # -> TSV on stdout")
    try:
        with open(sys.argv[1]) as f:
            sys.stdout.write(to_tsv(parse(f.read())))
    except FwmapError as e:
        sys.exit("fwmap: %s" % e)
