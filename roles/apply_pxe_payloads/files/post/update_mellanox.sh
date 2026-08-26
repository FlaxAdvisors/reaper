#!/bin/bash -x

fwdst=/tmp/

# PSID -> "fwdir|fwbin" (bin name WITHOUT .zip). Multiple PSIDs may map to the
# same image: an OEM-branded card (FB_/HP_) flashed with the Mellanox image is
# re-branded to the image's MT_ PSID via allow_psid_change. To support a new
# card, add one line here.
declare -A FWMAP=(
  # MCX4411A-ACQN family (Leopard) -- PRESERVED from the prior workablepsid()
  [MT_2450112034]="MCX4411A-ACQN|fw-ConnectX4Lx-rel-14_32_1010-MCX4411A-ACQ_Ax-UEFI-14.25.17-FlexBoot-3.6.502.bin"  # ACQN native
  [MT_2450111034]="MCX4411A-ACAN|fw-ConnectX4Lx-rel-14_32_1010-MCX4411A-ACA_Bx-UEFI-14.25.17-FlexBoot-3.6.502.bin"  # ACAN native
  [FB_2450111034]="MCX4411A-ACQN|fw-ConnectX4Lx-rel-14_32_1010-MCX4411A-ACQ_Ax-UEFI-14.25.17-FlexBoot-3.6.502.bin"
  [FB_0000000005]="MCX4411A-ACQN|fw-ConnectX4Lx-rel-14_32_1010-MCX4411A-ACQ_Ax-UEFI-14.25.17-FlexBoot-3.6.502.bin"
  # MCX4121A family (Tioga Pass) -- NEW
  [MT_2420110034]="MT_2420110034|fw-ConnectX4Lx-rel-14_32_1010-MCX4121A-ACA_Ax-UEFI-14.25.17-FlexBoot-3.6.502.bin"
  [HP_2420110034]="MT_2420110034|fw-ConnectX4Lx-rel-14_32_1010-MCX4121A-ACA_Ax-UEFI-14.25.17-FlexBoot-3.6.502.bin"
)

devuntouchable="secure-fw"
uefival=1
needbmcreset=0

# selected-image state (set by selectfw)
fwsrc=""
fwbin=""
binfwver=""
binpsid=""

source ./common_mellanox.sh

# Select the firmware image for a device PSID. Sets fwsrc/fwbin and returns 0
# when the PSID is mapped; logs + returns 1 (caller skips) when it is not.
function selectfw()
{
    local psid="$1"
    local entry="${FWMAP[$psid]}"
    if [ -z "$entry" ]; then
        echo "Unsupported PSID ($psid) -- no firmware mapping; skipping."
        return 1
    fi
    fwsrc="/export/share/mellanox/${entry%%|*}/"
    fwbin="${entry##*|}"
    return 0
}

# Fetch+unzip the selected image (idempotent: skip if already on disk, so two
# cards sharing an image fetch once). A failure skips THIS card, not the run.
function fetchfw()
{
    if [ -f "${fwdst}${fwbin}" ]; then return 0; fi
    cd "$fwdst" || return 1
    local fwurl="http://bang${fwsrc}${fwbin}.zip"
    if ! curl -kOLJ "$fwurl"; then
        echo "Unable to fetch fw bundle via url ($fwurl)."
        return 1
    fi
    unzip -o "${fwbin}.zip"
    rm -f "${fwbin}.zip"
}

function getbininfo()
{
    IFS=$'\n' && bininfo=$(mstflint -i ${fwdst}${fwbin} query)
    retval=$?
    if [ $retval -eq 0 ]; then
        binfwver=$(printf "%s\n" $bininfo|grep 'FW Version:'|cut -d':' -f2|sed 's/^\s*//')
        binpsid=$(printf "%s\n" $bininfo|grep 'PSID:'|cut -d':' -f2|sed 's/^\s*//')
        echo "binfw $binfwver binpsid $binpsid"
    fi
    return $retval
}

function needsverup()
{
    # Skip ONLY when the card matches the image on BOTH version AND PSID. A
    # PSID mismatch (an FB_/HP_ override not yet re-branded to the image's MT_
    # PSID) forces the flash even at the same FW version -- that is the
    # override's purpose. Self-terminating: after the re-brand devpsid==binpsid,
    # so it skips on every subsequent run.
    if [ "$devfwver" == "$binfwver" ] && [ "$devpsid" == "$binpsid" ]; then
        echo "FW ${binfwver} + PSID ${devpsid} already match image; skip"
        return 1
    fi
    return 0
}

function flashnicfw()
{
    devhere=$1
    allow_psid_change=1
    no_fw_ctrl=0
    domstflint burn $devhere ${fwdst}${fwbin}
    domstfwreset $devhere
    sleep 5
}

function needuefi()
{
    devhere=$1
    uefival=$(domstconfig query $devhere | grep "EXP_ROM_UEFI_x86_ENABLE" | sed -re 's/^\s+EXP_ROM_UEFI_x86_ENABLE\s+\S+\(([01])\)\s*$/\1/')
    if [ -z "$uefival" ]; then
        return 1
    fi
    return $uefival
}

function setuefi()
{
    devhere=$1
    domstconfig set $devhere "EXP_ROM_UEFI_x86_ENABLE=true"
    domstfwreset $devhere
    if [ $needbmcreset -eq 0 ]; then
        needbmcreset=1
    fi
    sleep 5
}

function bmcresetcold()
{
    ipmitool mc reset cold
}

####
#### Begin Workflow
####

getpcidev
checkmstflint

# loop over mlx devices; select+fetch each card's image by its PSID
for mlxdev in $(printf "%s\n" $pcidev); do
    getdevinfo $mlxdev
    if ! selectfw "$devpsid"; then continue; fi
    if ! fetchfw; then continue; fi
    if ! getbininfo; then echo "Unable to query bin ($fwbin)"; continue; fi
    if [ "$devsecure" == "$devuntouchable" ]; then continue; fi
    if needsverup; then flashnicfw $mlxdev; fi
    if needuefi ${mlxdev}; then setuefi ${mlxdev}; fi
done

if [ $needbmcreset -ne 0 ]; then
    bmcresetcold
fi
