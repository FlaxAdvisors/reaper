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
# NO `set -u`. common_mellanox.sh is shared verbatim with the post lane and its
# domstflint() assigns binfile=$3 while getdevinfo() calls it with two args --
# fatal under -u. That cost the first three live runs on 2026-09-18: every card
# query died, nothing was flashed, and IDENT still blinked "done", so a card
# went back into service still locked. update_mellanox.sh has always run
# without -u; the station matches it. See tests/test_unlock_shell_compat.py.

here=$(cd "$(dirname "$0")" && pwd)
cd "$here" || exit 1

logdir=/var/log/flax/mezz-flash
mkdir -p "$logdir"
log="$logdir/$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$log") 2>&1

echo "=== unlock_mellanox.sh $(date -u +%FT%TZ) ==="
[ -f MANIFEST ] && cat MANIFEST

# PROTECT_PCI is read by mezz_select.py; SHUTDOWN_ON_DONE is read here.
[ -f /etc/flax/mezz-flash.conf ] && . /etc/flax/mezz-flash.conf

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
# common_mellanox.sh's getdevinfo() greps an UNANCHORED 'PSID:', which also
# matches the 'Orig PSID:' line mstflint prints once a card's PSID has been
# changed -- so $devpsid comes back holding BOTH values, newline-joined. Every
# later comparison then fails: the FWMAP lookup misses and an already-unlocked
# card reads as unknown. Re-derive it from the same query output with an
# anchored match. common_mellanox.sh is shared verbatim with the post lane and
# is not ours to edit (see the header), so the narrowing happens here.
function narrowpsid()
{
    local only
    only=$(printf "%s\n" $devinfo | grep '^PSID:' | cut -d':' -f2 | sed 's/^\s*//')
    [ -n "$only" ] && devpsid="$only"
}

# Who this card IS. A BDF names the SLOT -- every card the station handles sits
# in the same one -- so a log keyed on it cannot be read back as a history:
# reviewing 2026-09-21 showed four burn attempts at 5e:00.0 with no way to tell
# one card retried four times from four cards. mstflint already reports the
# identity in the query getdevinfo() parses, so this costs one more grep.
function cardident()
{
    devmac=$(printf "%s\n" $devinfo | grep '^Base MAC:' | awk '{print $3}')
    devguid=$(printf "%s\n" $devinfo | grep '^Base GUID:' | awk '{print $3}')
    devmac="${devmac:-unknown}"
    devguid="${devguid:-unknown}"
}

# The burn pass. Every "we cannot burn this" exit is a `return`, never the
# loop's `continue`: a card we have no image for is still a card that has to
# PXE boot when it goes into service, so it must still reach the UEFI pass.
# That coupling is exactly what cost et26b3 its UEFI ROM on 2026-09-21.
function burnpass()
{
    local mlxdev=$1
    local entry fwdir fwbin img want have bininfo binfwver binpsid

    entry="${FWMAP[$devpsid]:-}"
    if [ -z "$entry" ]; then
        echo "$mlxdev: PSID $devpsid is not in the map; no burn."
        return 0
    fi

    fwdir="${entry%%|*}"
    fwbin="${entry##*|}"
    img="fw/${fwdir}/${fwbin}"
    if [ ! -f "$img" ]; then
        echo "$mlxdev: image missing from bundle ($img); no burn."
        return 0
    fi
    # The rootfs takes a hard power cut every cycle (blade pulled while up), so
    # a truncated image is a real possibility, not a theoretical one.
    want=$(awk -v f="$img" '$2 == f {print $1}' SHA256SUMS)
    have=$(sha256sum "$img" | cut -d' ' -f1)
    if [ -z "$want" ] || [ "$want" != "$have" ]; then
        echo "$mlxdev: checksum FAILED for $img (want=${want:-<absent>} have=$have); refusing to burn."
        return 0
    fi

    binfwver=""; binpsid=""
    IFS=$'\n' bininfo=$(mstflint -i "$img" query)
    binfwver=$(printf "%s\n" $bininfo | grep 'FW Version:' | cut -d':' -f2 | sed 's/^\s*//')
    binpsid=$(printf "%s\n" $bininfo | grep 'PSID:' | cut -d':' -f2 | sed 's/^\s*//')
    echo "$mlxdev: dev fw=$devfwver psid=$devpsid sec=[$devsecure] | img fw=$binfwver psid=$binpsid"

    if [ "$devfwver" == "$binfwver" ] && [ "$devpsid" == "$binpsid" ]; then
        echo "$mlxdev: already at target FW+PSID; no burn."
        return 0
    fi

    domstflint burn "$mlxdev" "$img"
    cardburned=yes

    # mstfwreset's exit status is the difference between "the new image is
    # running" and "the new image is merely in flash". It fails on these cards
    # with ME_MAD_SEND_FAILED(8) -- mstflint says so itself ("Failed to update
    # FW boot address. Power cycle the device in order to load the new FW") --
    # and the old code ignored it and carried on as though the card had
    # activated. It had not: the config the UEFI pass then read still belonged
    # to the OUTGOING image. Record it so the log can say the card needs
    # another pass rather than leaving that to be inferred from a warning
    # buried in mstflint's output.
    if domstfwreset "$mlxdev"; then
        cardactivated=yes
    else
        cardactivated=no
        echo "$mlxdev: WARNING mstfwreset failed -- new FW is in flash but NOT"
        echo "$mlxdev: running. The card needs a cold power cycle, then a"
        echo "$mlxdev: second station pass to finish (config below is stale)."
    fi
    sleep 5
    # The record that proves the lock cleared: Security Attributes should no
    # longer carry secure-fw once the unlocked image is on the card.
    getdevinfo "$mlxdev"
    narrowpsid
    cardident
    echo "$mlxdev: POST-FLASH fw=$devfwver psid=$devpsid sec=[$devsecure]"
}

# The UEFI expansion ROM pass -- the card must PXE boot once it goes into
# service, and the fleet standard is UEFI, not legacy-PXE-only.
#
# Fails CLOSED. An unreadable config query is the case with the LEAST evidence
# the ROM is on, so it must set it rather than assume it. The old
# `${uefival:-1}` had that exactly backwards, and it is how the 06:13-07:41
# runs on et26b3 passed over a card whose ROM was off: mstfwreset had failed
# with ME_MAD_SEND_FAILED(8), so the config being read still belonged to the
# outgoing locked image. `mstconfig set` writes the Next Boot column and is
# idempotent, so re-setting an already-enabled card costs nothing.
function uefipass()
{
    local mlxdev=$1
    local uefival
    uefival=$(domstconfig query "$mlxdev" | grep "EXP_ROM_UEFI_x86_ENABLE" \
        | sed -re 's/^\s+EXP_ROM_UEFI_x86_ENABLE\s+\S+\(([01])\)\s*$/\1/')
    # A card whose new image is in flash but not running reports the OUTGOING
    # image's config, so "already on" is not evidence about the image the card
    # will actually boot. Set it regardless and let the second pass confirm.
    if [ "$uefival" == "1" ] && [ "$cardactivated" != "no" ]; then
        echo "$mlxdev: EXP_ROM_UEFI_x86_ENABLE already set"
        carduefi=already
        return 0
    fi
    if [ -z "$uefival" ]; then
        echo "$mlxdev: could not read EXP_ROM_UEFI_x86_ENABLE; setting it anyway"
    elif [ "$cardactivated" == "no" ]; then
        echo "$mlxdev: card not activated; setting EXP_ROM_UEFI_x86_ENABLE regardless"
    else
        echo "$mlxdev: enabling EXP_ROM_UEFI_x86_ENABLE"
    fi
    domstconfig set "$mlxdev" "EXP_ROM_UEFI_x86_ENABLE=true"
    carduefi=set
    domstfwreset "$mlxdev"
    needbmcreset=1
    sleep 5
}

for mlxdev in $targets; do
    echo "--- $mlxdev ---"
    if ! getdevinfo "$mlxdev"; then
        echo "$mlxdev: query failed; skipping."
        continue
    fi
    narrowpsid
    cardident
    # Per-card state the two passes report back through.
    cardburned=no
    cardactivated=n/a
    carduefi=unknown
    echo "$mlxdev: card mac=$devmac guid=$devguid psid=$devpsid fw=$devfwver sec=[$devsecure]"

    burnpass "$mlxdev"
    uefipass "$mlxdev"

    # ONE greppable line per card per run -- this is the station's history.
    # `grep RESULT /var/log/flax/mezz-flash/*.log` answers "was this card ever
    # unlocked, and did it get its UEFI ROM" without reading any prose.
    echo "$mlxdev: RESULT mac=$devmac guid=$devguid psid=$devpsid fw=$devfwver" \
         "sec=[$devsecure] burned=$cardburned activated=$cardactivated uefi=$carduefi"
    if [ "$cardactivated" == "no" ]; then
        echo "$mlxdev: NEEDS-SECOND-PASS mac=$devmac -- cold power cycle, then re-run"
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

# Identify first, then power off -- the same order post.sh uses, where it is
# known to survive the transition (the BMC runs on standby power, so the blink
# outlives the host). Reversed, the blade goes dark before the LED is set and
# the operator has no signal at all.
#
# NOT `ipmitool chassis power off`, which post.sh uses: that is an immediate
# hard off, harmless there because post runs from a RAM-based live ISO with no
# disk to dirty. This station has a real rootfs that is otherwise hard-cut on
# every blade pull, and avoiding that is the whole point of powering down.
#
# always-on is a resume-after-power-loss policy, not a "must always be on"
# rule, so a deliberate shutdown does not fight it: re-seating the blade
# re-applies power and the BMC brings the host back up.
if [ "${SHUTDOWN_ON_DONE:-1}" = "1" ]; then
    echo "powering off cleanly (rootfs stays consistent across the pull)"
    sync
    shutdown -h now
else
    echo "SHUTDOWN_ON_DONE=0 -- staying up (dev: log readable over ssh)"
fi
