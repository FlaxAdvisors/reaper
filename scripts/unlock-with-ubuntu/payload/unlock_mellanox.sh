#!/bin/bash
# unlock_mellanox.sh -- the unlock station's flash pass.
#
# Derived from roles/apply_pxe_payloads/files/post/update_mellanox.sh, with
# four deliberate differences. Do not "fix" these back toward the original:
#
#   1. NO NETWORK. Images come from the bundle's ./fw/<PSID>/ instead of
#      curl http://bang/... -- a jumpered mezzanine card passes no traffic, and
#      the deployed station has no other NIC at all.
#   2. NO secure-fw SKIP. The original skips any card reporting secure-fw
#      because mstflint cannot burn it. Clearing that lock -- with the override
#      jumper fitted -- is this station's entire purpose.
#   3. The PSID->image map is DATA (nic_fw_map.tsv), extracted from the
#      original's FWMAP at build time. Never hand-edit it here; fix FWMAP and
#      rebuild, or the station and the fleet disagree about what a card should be.
#   4. Ends with `ipmitool chassis identify force` -- an indefinite blink that
#      means DONE, the operator's only signal at the rack. Solid blue is
#      power-on and means nothing about this script.
#
# Pass/fail is NOT distinguishable at the rack by design (operator decision):
# a failure shows up later as a card whose lock is still set when it goes into
# service. The log is the record; read it over ssh while a working NIC is in.
set -u

here=$(cd "$(dirname "$0")" && pwd)
cd "$here" || exit 1

logdir=/var/log/flax/mezz-flash
mkdir -p "$logdir"
log="$logdir/$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$log") 2>&1

echo "=== unlock_mellanox.sh $(date -u +%FT%TZ) ==="
[ -f MANIFEST ] && cat MANIFEST

# globals consumed by common_mellanox.sh's domstflint
allow_psid_change=1
no_fw_ctrl=0
needbmcreset=0

source ./common_mellanox.sh

# --- map ------------------------------------------------------------------
declare -A FWMAP=()
while IFS=$'\t' read -r psid fwdir fwbin; do
    [ -n "${psid:-}" ] || continue
    FWMAP["$psid"]="${fwdir}|${fwbin}"
done < nic_fw_map.tsv
echo "map: ${#FWMAP[@]} PSIDs"
if [ "${#FWMAP[@]}" -eq 0 ]; then
    echo "FATAL: empty nic_fw_map.tsv -- bad bundle, flashing nothing."
    exit 1
fi

# --- which cards ----------------------------------------------------------
# Cards holding an IPv4 address are skipped: on a development station the PCIe
# uplink is a ConnectX too, and resetting it mid-run drops the operator's ssh
# session. Not a safety gate -- a flashed uplink lands at the same FW+UEFI as
# everything else -- just a convenience. MEZZ_FLASH_ALL=1 disables it.
if [ "${MEZZ_FLASH_ALL:-0}" = "1" ]; then
    targets=$(lspci -d "15b3:" | cut -d " " -f1 | grep '00\.0$')
    echo "MEZZ_FLASH_ALL=1 -- every Mellanox card is a target"
else
    targets=$(python3 ./mezz_select.py)
fi
echo "targets: ${targets:-<none>}"
if [ -z "$targets" ]; then
    echo "No target cards. Nothing to flash."
fi

# --- flash ----------------------------------------------------------------
for mlxdev in $targets; do
    echo "--- $mlxdev ---"
    if ! getdevinfo "$mlxdev"; then
        echo "$mlxdev: query failed; skipping."
        continue
    fi
    entry="${FWMAP[$devpsid]:-}"
    if [ -z "$entry" ]; then
        echo "$mlxdev: PSID $devpsid is not in the map; skipping."
        continue
    fi
    fwdir="${entry%%|*}"
    fwbin="${entry##*|}"
    img="fw/${fwdir}/${fwbin}"
    if [ ! -f "$img" ]; then
        echo "$mlxdev: image missing from bundle ($img); skipping."
        continue
    fi
    # The rootfs takes a hard power cut every cycle (blade pulled while up), so
    # a truncated image is a real possibility, not a theoretical one.
    want=$(awk -v f="$img" '$2 == f {print $1}' SHA256SUMS)
    have=$(sha256sum "$img" | cut -d' ' -f1)
    if [ -z "$want" ] || [ "$want" != "$have" ]; then
        echo "$mlxdev: checksum FAILED for $img (want=${want:-<absent>} have=$have); refusing to burn."
        continue
    fi

    binfwver=""; binpsid=""
    IFS=$'\n' bininfo=$(mstflint -i "$img" query)
    binfwver=$(printf "%s\n" $bininfo | grep 'FW Version:' | cut -d':' -f2 | sed 's/^\s*//')
    binpsid=$(printf "%s\n" $bininfo | grep 'PSID:' | cut -d':' -f2 | sed 's/^\s*//')
    echo "$mlxdev: dev fw=$devfwver psid=$devpsid sec=[$devsecure] | img fw=$binfwver psid=$binpsid"

    if [ "$devfwver" == "$binfwver" ] && [ "$devpsid" == "$binpsid" ]; then
        echo "$mlxdev: already at target FW+PSID; no burn."
    else
        domstflint burn "$mlxdev" "$img"
        domstfwreset "$mlxdev"
        sleep 5
        # The record that proves the lock cleared: Security Attributes should no
        # longer carry secure-fw once the unlocked image is on the card.
        getdevinfo "$mlxdev"
        echo "$mlxdev: POST-FLASH fw=$devfwver psid=$devpsid sec=[$devsecure]"
    fi

    # UEFI expansion ROM -- the card must PXE boot once it goes into service.
    uefival=$(domstconfig query "$mlxdev" | grep "EXP_ROM_UEFI_x86_ENABLE" \
        | sed -re 's/^\s+EXP_ROM_UEFI_x86_ENABLE\s+\S+\(([01])\)\s*$/\1/')
    if [ "${uefival:-1}" == "0" ]; then
        echo "$mlxdev: enabling EXP_ROM_UEFI_x86_ENABLE"
        domstconfig set "$mlxdev" "EXP_ROM_UEFI_x86_ENABLE=true"
        domstfwreset "$mlxdev"
        needbmcreset=1
        sleep 5
    fi
done

# --- signal ---------------------------------------------------------------
# Ordering matters: a BMC cold reset clears identify state, so it must happen
# BEFORE the blink is set, and the BMC must be answering again first. Reversed,
# the blade sits dark and finished-looking-like-still-working.
if [ $needbmcreset -ne 0 ]; then
    echo "cold-resetting BMC after UEFI change"
    ipmitool mc reset cold
    for _ in $(seq 1 30); do
        sleep 5
        if ipmitool mc info >/dev/null 2>&1; then echo "BMC back"; break; fi
    done
fi

# Keep "insert the blade and it boots" true across BMC resets.
ipmitool chassis policy always-on || true

echo "DONE -- lighting identify (indefinite blink)"
ipmitool chassis identify force
echo "=== end $(date -u +%FT%TZ) ==="
