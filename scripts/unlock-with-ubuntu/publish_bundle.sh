#!/bin/bash
# publish_bundle.sh <tgz> <version> <dir> -- publish a built station bundle
# for self-update (payload/self_update.sh) as <dir>/mezz-flash.tgz and
# <dir>/mezz-flash.version. Called by create-unlock-with-ubuntu.sh when
# PUBLISH= is set; on a bang <dir> is /srv/pxe, served on every lab VLAN's
# gateway address by the pxe nginx vhost's catch-all `location /`.
#
# Both are written tmp + mv, the tarball FIRST and the version file LAST: a
# station that reads the new version must find the new tarball complete
# behind it, or it fails the checksum and runs its old bundle for a boot.
#
# HA pair: build ONCE and copy both files to the peer bang. Two builds carry
# two different build timestamps, and a station would reinstall on every VIP
# move.
set -euo pipefail

[ $# -eq 3 ] || { echo "usage: $0 <tgz> <version> <dir>" >&2; exit 2; }
tgz=$1 ver=$2 dir=$3

sha=$(sha256sum "$tgz" | awk '{print $1}')
size=$(stat -c %s "$tgz")

cp "$tgz" "$dir/.mezz-flash.tgz.tmp"
chmod 0644 "$dir/.mezz-flash.tgz.tmp"
mv -f "$dir/.mezz-flash.tgz.tmp" "$dir/mezz-flash.tgz"

printf 'version=%s\nsha256=%s\nsize=%s\n' "$ver" "$sha" "$size" \
    > "$dir/.mezz-flash.version.tmp"
chmod 0644 "$dir/.mezz-flash.version.tmp"
mv -f "$dir/.mezz-flash.version.tmp" "$dir/mezz-flash.version"

echo "published $ver ($size bytes, sha256 $sha) to $dir"
