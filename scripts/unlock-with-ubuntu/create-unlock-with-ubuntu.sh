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
# PUBLISH=/srv/pxe additionally publishes it as mezz-flash.tgz +
# mezz-flash.version there, which every deployed station checks before each
# flash pass (payload/self_update.sh). On an HA pair, build on one bang and
# copy both files to the other (publish_bundle.sh says why). Needs write
# access to that dir -- run the build with sudo -E, or copy afterwards.
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
publish="${PUBLISH:-}"
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

echo "== OPN -> PSID map from the share's symlinks =="
# The fallback route for a card whose PSID has no FWMAP row. Only three
# OEM-branded PSIDs are catalogued, but every card reports its own part number,
# and the share keeps an OPN symlink beside each PSID dir
# (MCX4411A-ACQ -> MT_2450112034). Building the map from those links lets the
# station resolve a lock nobody has catalogued instead of walking away from it.
( cd "$share" && for l in *; do
    [ -L "$l" ] || continue
    printf '%s\t%s\n' "$l" "$(readlink "$l")"
  done ) | sort > "$build/nic_opn_map.tsv"
opns=$(wc -l < "$build/nic_opn_map.tsv")
echo "   $opns OPNs"
# Same call as the FWMAP extraction: a silently empty map yields a bundle that
# installs cleanly and then quietly loses the coverage it was built for.
if [ "$opns" -eq 0 ]; then
    echo "FATAL: no OPN symlinks under $share -- the OPN fallback would be dead."
    exit 1
fi

echo "== payload =="
cp -a "$here/payload/." "$build/"
# Belt-and-braces for station_ident.py's mode: `cp -a` and tar both preserve
# whatever mode git checked it out with, so a mode regression in the repo
# (100644 instead of 100755) would otherwise ride silently into every bundle
# and fail closed as a bare "Permission denied" on the blade. Both call sites
# (this build and deploy.sh) run it directly.
chmod 0755 "$build/station_ident.py"
# A local test run of mezz_select.py leaves __pycache__ behind in the source
# tree; it must not ride along into the bundle.
find "$build" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$build" -name "*.pyc" -delete 2>/dev/null || true
# common_mellanox.sh is shared verbatim with the post lane -- the station uses
# its domstflint/domstconfig/domstfwreset/getdevinfo helpers, but deliberately
# NOT its getpcidev (which enumerates every ConnectX in the box).
cp "$src/common_mellanox.sh" "$build/common_mellanox.sh"

echo "== blade identity: FRU parser + family map =="
# VERBATIM copies, refreshed at build time -- the same call this script already
# makes for FWMAP. A station that identifies a blade differently from the fleet
# would put a run on the wrong tile, so drift is a build failure, not a
# runtime surprise. reaper-devel tests/test_fru_copies_drift.py pins the bytes.
# This overwrites the committed payload/flaxfru/ copy on purpose: the payload
# tree can go stale between commits, this extraction cannot.
mkdir -p "$build/flaxfru"
: > "$build/flaxfru/__init__.py"
for m in fru family_map; do
    src_mod="$repo/flax_observe/$m.py"
    [ -f "$src_mod" ] || { echo "FATAL: no $src_mod"; exit 1; }
    cp "$src_mod" "$build/flaxfru/$m.py"
done

# The family map is per-SITE and decides which FRU field is the ship serial
# (leopard -> Chassis Serial, everyone else -> Product Serial). This script
# already runs on a bang, so take that bang's deployed map.
fmdir="${FAMILY_MAP:-/etc/flax/family-map}"
[ -d "$fmdir" ] || { echo "FATAL: family map not found at $fmdir (run this on a bang, or set FAMILY_MAP=)"; exit 1; }
mkdir -p "$build/family-map"
cp "$fmdir"/*.txt "$build/family-map/" || { echo "FATAL: no family-map .txt files in $fmdir"; exit 1; }
echo "   $(ls "$build/family-map" | wc -l) families"

echo "== smoke test: exercise what was just assembled =="
# This would have caught C1 (station_ident.py shipped 100644, so every
# deploy.sh aborted at Permission denied on every blade) at BUILD time
# instead of on a rack. rc 0 (identified) or 1 (no_serial/no family match --
# this build host need not itself be a flashable blade) are both fine; any
# other code -- 126 Permission denied included -- is not.
smoke_rc=0
python3 "$build/station_ident.py" >/dev/null 2>&1 || smoke_rc=$?
if [ "$smoke_rc" != 0 ] && [ "$smoke_rc" != 1 ]; then
    echo "FATAL: station_ident.py smoke test exited $smoke_rc (want 0 or 1)"
    exit 1
fi
( cd "$build" && python3 -c 'import flaxfru.fru, flaxfru.family_map' )

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

# `version` is what a station compares against the published
# mezz-flash.version to decide whether to update. -dirty marks a build from
# uncommitted changes, so one never passes for the commit it sits on.
repover=$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)
if [ "$repover" != unknown ] && [ -n "$(git -C "$repo" status --porcelain 2>/dev/null)" ]; then
    repover="$repover-dirty"
fi
built=$(date -u +%Y%m%dT%H%M%SZ)
version="$repover@$built"

cat > "$build/MANIFEST" <<EOF
bundle : unlock-with-ubuntu
version : $version
built  : $built
host   : $(hostname)
repo   : $repover
psids  : $psids
opns   : $opns
images : $count
EOF
cat "$build/MANIFEST"

echo "== tarball =="
tar -C "$build" -czf "$out/unlock-with-ubuntu.tgz" .
ls -lh "$out/unlock-with-ubuntu.tgz"

if [ -n "$publish" ]; then
    echo "== publish -> $publish =="
    "$here/publish_bundle.sh" "$out/unlock-with-ubuntu.tgz" "$version" "$publish"
fi

cat <<EOF

Install on a node that still has a working NIC:
  scp $out/unlock-with-ubuntu.tgz <node>:.
  ssh <node> 'mkdir -p unlock && tar -C unlock -xf unlock-with-ubuntu.tgz && cd unlock && sudo ./deploy.sh'
EOF
