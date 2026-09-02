#!/bin/bash -x

fwdst=/tmp/

# PSID -> "fwdir|fwbin" (bin name WITHOUT .zip). fwdir is a directory under
# /export/share/mellanox served over HTTP by bang; it is the canonical MT_
# PSID directory, with a human-readable OPN symlink sitting beside it. For a
# native card fwdir therefore equals the PSID key -- if the two columns of an
# MT_ row disagree, the row is wrong.
#
# Multiple PSIDs may map to the same image: an OEM-branded card (FB_/HP_)
# flashed with the Mellanox image is re-branded to the image's MT_ PSID via
# allow_psid_change. To support a new branded card, add one line pointing at
# the MT_ image it should become.
#
# Coverage: every ConnectX-4 Lx EN OPN at 14.32.1912 -- the family's final
# firmware release -- 12 single-port and 12 dual-port. Provenance (part
# numbers, SHA256, NVIDIA descriptions) is in
# /export/share/mellanox/cx4lx-14_32_1912.tsv.
FWREL=14_32_1912
FWSFX=UEFI-14.25.17-FlexBoot-3.6.502.bin
declare -A FWMAP=(
  # -- PCIe stand-up, single-port ------------------------------------------
  [MT_2410110034]="MT_2410110034|fw-ConnectX4Lx-rel-${FWREL}-MCX4111A-ACA_Ax-${FWSFX}"   # MCX4111A-ACA   25GbE SFP28
  [MT_0000000267]="MT_0000000267|fw-ConnectX4Lx-rel-${FWREL}-MCX4111A-ACUT_Ax-${FWSFX}"  # MCX4111A-ACUT  25GbE SFP28, UEFI, tall bracket
  [MT_2410110004]="MT_2410110004|fw-ConnectX4Lx-rel-${FWREL}-MCX4111A-XCA_Ax-${FWSFX}"   # MCX4111A-XCA   10GbE SFP28
  # -- PCIe stand-up, dual-port --------------------------------------------
  [MT_2420110034]="MT_2420110034|fw-ConnectX4Lx-rel-${FWREL}-MCX4121A-ACA_Ax-${FWSFX}"   # MCX4121A-ACA   25GbE SFP28   (Tioga Pass)
  [MT_0000000647]="MT_0000000647|fw-ConnectX4Lx-rel-${FWREL}-MCX4121A-ACH_Ax-${FWSFX}"   # MCX4121A-ACH   25GbE SFP28, host mgmt
  [MT_0000000266]="MT_0000000266|fw-ConnectX4Lx-rel-${FWREL}-MCX4121A-ACU_Ax-${FWSFX}"   # MCX4121A-ACU   25GbE SFP28, UEFI
  [MT_2420110004]="MT_2420110004|fw-ConnectX4Lx-rel-${FWREL}-MCX4121A-XCA_Ax-${FWSFX}"   # MCX4121A-XCA   10GbE SFP28
  [MT_0000000414]="MT_0000000414|fw-ConnectX4Lx-rel-${FWREL}-MCX4121A-XCH_Ax-${FWSFX}"   # MCX4121A-XCH   10GbE SFP28, host mgmt
  # -- PCIe stand-up, single-port QSFP28 -----------------------------------
  [MT_2430110027]="MT_2430110027|fw-ConnectX4Lx-rel-${FWREL}-MCX4131A-BCA_Ax-${FWSFX}"   # MCX4131A-BCA   40GbE QSFP28
  [MT_2430110032]="MT_2430110032|fw-ConnectX4Lx-rel-${FWREL}-MCX4131A-GCA_Ax-${FWSFX}"   # MCX4131A-GCA   50GbE QSFP28
  # -- OCP 2.0, single-port -------------------------------------------------
  [MT_2450111034]="MT_2450111034|fw-ConnectX4Lx-rel-${FWREL}-MCX4411A-ACA_Bx-${FWSFX}"   # MCX4411A-ACA   25GbE SFP28   (Leopard, ACAN native)
  [MT_0000000501]="MT_0000000501|fw-ConnectX4Lx-rel-${FWREL}-MCX4411A-ACH_Ax-${FWSFX}"   # MCX4411A-ACH   25GbE SFP28, Type 1, host mgmt
  [MT_2450112034]="MT_2450112034|fw-ConnectX4Lx-rel-${FWREL}-MCX4411A-ACQ_Ax-${FWSFX}"   # MCX4411A-ACQ   25GbE SFP28, host mgmt (Leopard, ACQN native)
  [MT_0000000268]="MT_0000000268|fw-ConnectX4Lx-rel-${FWREL}-MCX4411A-ACUN_Ax-${FWSFX}"  # MCX4411A-ACUN  25GbE SFP28, no host mgmt, UEFI
  # -- OCP 2.0, dual-port ---------------------------------------------------
  [MT_2470111034]="MT_2470111034|fw-ConnectX4Lx-rel-${FWREL}-MCX4421A-ACA_Bx-${FWSFX}"   # MCX4421A-ACA   25GbE SFP28
  [MT_2470112034]="MT_2470112034|fw-ConnectX4Lx-rel-${FWREL}-MCX4421A-ACQ_Ax-${FWSFX}"   # MCX4421A-ACQ   25GbE SFP28, host mgmt
  [MT_0000000275]="MT_0000000275|fw-ConnectX4Lx-rel-${FWREL}-MCX4421A-ACU_Ax-${FWSFX}"   # MCX4421A-ACU   25GbE SFP28, no host mgmt, UEFI
  [MT_0000000588]="MT_0000000588|fw-ConnectX4Lx-rel-${FWREL}-MCX4421A-XCH_Ax-${FWSFX}"   # MCX4421A-XCH   10GbE SFP28, Type 1, host mgmt
  [MT_2470110004]="MT_2470110004|fw-ConnectX4Lx-rel-${FWREL}-MCX4421A-XCQ_Ax-${FWSFX}"   # MCX4421A-XCQ   10GbE SFP28, host mgmt
  # -- OCP 2.0, single-port QSFP28 ------------------------------------------
  [MT_2490111032]="MT_2490111032|fw-ConnectX4Lx-rel-${FWREL}-MCX4431A-GCA_Bx-${FWSFX}"   # MCX4431A-GCA   50GbE QSFP28
  [MT_0000000506]="MT_0000000506|fw-ConnectX4Lx-rel-${FWREL}-MCX4431A-GCU_Ax-${FWSFX}"   # MCX4431A-GCU   50GbE QSFP28, Type 1, host mgmt
  [MT_2510111032]="MT_2510111032|fw-ConnectX4Lx-rel-${FWREL}-MCX4431M-GCA_Bx-${FWSFX}"   # MCX4431M-GCA   50GbE QSFP28, multi-host
  # -- OCP 3.0, dual-port ---------------------------------------------------
  [MT_0000000238]="MT_0000000238|fw-ConnectX4Lx-rel-${FWREL}-MCX4621A-ACA_Ax-${FWSFX}"   # MCX4621A-ACA   25GbE SFP28, host mgmt
  [MT_0000000537]="MT_0000000537|fw-ConnectX4Lx-rel-${FWREL}-MCX4621A-XCA_Ax-${FWSFX}"   # MCX4621A-XCA   10GbE SFP28, host mgmt
  # -- OEM-branded overrides: re-branded to the target MT_ PSID on flash ----
  [FB_2450111034]="MT_2450112034|fw-ConnectX4Lx-rel-${FWREL}-MCX4411A-ACQ_Ax-${FWSFX}"   # -> MCX4411A-ACQ
  [FB_0000000005]="MT_2450112034|fw-ConnectX4Lx-rel-${FWREL}-MCX4411A-ACQ_Ax-${FWSFX}"   # -> MCX4411A-ACQ
  [HP_2420110034]="MT_2420110034|fw-ConnectX4Lx-rel-${FWREL}-MCX4121A-ACA_Ax-${FWSFX}"   # -> MCX4121A-ACA
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
