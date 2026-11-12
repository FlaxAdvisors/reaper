#!/bin/sh
# Build the site's iPXE network boot programs from a pinned upstream commit + our
# patches + our embedded script. Usage: build-ipxe.sh <workdir>
# Outputs <workdir>/out/efi64/ipxe.efi and <workdir>/out/bios/undionly.kpxe (+ md5s).
# Needs git, gcc, make, binutils, perl, xz/liblzma headers (a bang has them).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
. "$here/ipxe.conf"
work=${1:?usage: build-ipxe.sh <workdir>}
mkdir -p "$work"
src="$work/ipxe"
[ -d "$src/.git" ] || git clone -q "$IPXE_REPO" "$src"
cd "$src"
git cat-file -e "$IPXE_COMMIT^{commit}" 2>/dev/null || git fetch -q origin
git checkout -q -f "$IPXE_COMMIT"
git clean -qfdx
for p in "$here"/patches/*.patch; do
    git apply "$p"
done
cd src
jobs=$(nproc 2>/dev/null || echo 4)
make -j"$jobs" bin-x86_64-efi/ipxe.efi EMBED="$here/chain.ipxe"
make -j"$jobs" bin/undionly.kpxe EMBED="$here/chain.ipxe"
rm -rf "$work/out"
mkdir -p "$work/out/efi64" "$work/out/bios"
cp bin-x86_64-efi/ipxe.efi "$work/out/efi64/ipxe.efi"
cp bin/undionly.kpxe "$work/out/bios/undionly.kpxe"
cd "$work/out" && md5sum efi64/ipxe.efi bios/undionly.kpxe | tee MD5SUMS
