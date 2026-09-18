#!/bin/bash
# create-unlock-with-ubuntu.sh -- build the mezzanine FW-unlock station bundle.
#
# Run this ON A BANG, where /export/share/mellanox is a local path -- the
# firmware is ~60MB and there is no reason to drag it across the wire:
#
#   ssh bang-gouda
#   cd ~/git/reaper && git pull
#   scripts/unlock-with-ubuntu/create-unlock-with-ubuntu.sh
#   # -> /tmp/unlock-with-ubuntu/unlock-with-ubuntu.tgz
#
# The resulting tarball is self-contained: scripts, systemd unit, and every
# firmware image the map names. It is a BUILD ARTIFACT and is gitignored --
# same call the repo already made for post.tgz. Never commit it.
#
# The PSID->image map is EXTRACTED from the post lane's update_mellanox.sh
# rather than copied, so the station cannot drift from what actually burns
# cards in the fleet. A parse failure aborts the build: a bundle with an empty
# map installs cleanly and then silently flashes nothing, and the station's
# only output is a "done" blink.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
src="$repo/roles/apply_pxe_payloads/files/post"
share="${SHARE:-/export/share/mellanox}"
out="${OUT:-/tmp/unlock-with-ubuntu}"
build="$out/build"

echo "repo   : $repo"
echo "share  : $share"
echo "out    : $out"

[ -f "$src/update_mellanox.sh" ] || { echo "FATAL: no $src/update_mellanox.sh"; exit 1; }
[ -d "$share" ] || { echo "FATAL: firmware share not found at $share (run this on a bang, or set SHARE=)"; exit 1; }

rm -rf "$build"
mkdir -p "$build/fw"

echo "== extracting FWMAP from update_mellanox.sh =="
python3 "$here/fwmap.py" "$src/update_mellanox.sh" > "$build/nic_fw_map.tsv"
psids=$(wc -l < "$build/nic_fw_map.tsv")
echo "   $psids PSIDs"

echo "== payload =="
cp -a "$here/payload/." "$build/"
# A local test run of mezz_select.py leaves __pycache__ behind in the source
# tree; it must not ride along into the bundle.
find "$build" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$build" -name "*.pyc" -delete 2>/dev/null || true
# common_mellanox.sh is shared verbatim with the post lane -- the station uses
# its domstflint/domstconfig/domstfwreset/getdevinfo helpers, but deliberately
# NOT its getpcidev (which enumerates every ConnectX in the box).
cp "$src/common_mellanox.sh" "$build/common_mellanox.sh"

echo "== firmware images =="
# Extracted with python3's zipfile rather than unzip(1): python3 is already
# required for the map extraction, and depending on unzip too would mean the
# build only runs where someone happened to install it.
count=$(python3 - "$build/nic_fw_map.tsv" "$share" "$build/fw" <<'PY'
import os, sys, zipfile
tsv, share, dst = sys.argv[1:4]
pairs = sorted({tuple(l.rstrip("\n").split("\t")[1:3])
                for l in open(tsv) if l.strip()})
for fwdir, fwbin in pairs:
    zpath = os.path.join(share, fwdir, fwbin + ".zip")
    if not os.path.isfile(zpath):
        sys.exit("FATAL: image missing from share: %s" % zpath)
    os.makedirs(os.path.join(dst, fwdir), exist_ok=True)
    with zipfile.ZipFile(zpath) as zf:
        names = [n for n in zf.namelist() if os.path.basename(n) == fwbin]
        if not names:
            sys.exit("FATAL: %s not found inside %s (has: %s)"
                     % (fwbin, zpath, ", ".join(zf.namelist()[:5])))
        with zf.open(names[0]) as fh, open(os.path.join(dst, fwdir, fwbin), "wb") as out:
            out.write(fh.read())
print(len(pairs))
PY
)
echo "   $count images"

echo "== checksums =="
( cd "$build" && find fw -type f | sort | xargs sha256sum > SHA256SUMS )

cat > "$build/MANIFEST" <<EOF
bundle : unlock-with-ubuntu
built  : $(date -u +%FT%TZ)
host   : $(hostname)
repo   : $(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)
psids  : $psids
images : $count
EOF
cat "$build/MANIFEST"

echo "== tarball =="
tar -C "$build" -czf "$out/unlock-with-ubuntu.tgz" .
ls -lh "$out/unlock-with-ubuntu.tgz"

cat <<EOF

Install on a node that still has a working NIC:
  scp $out/unlock-with-ubuntu.tgz <node>:.
  ssh <node> 'mkdir -p unlock && tar -C unlock -xf unlock-with-ubuntu.tgz && cd unlock && sudo ./deploy.sh'
EOF
