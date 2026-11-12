#!/bin/bash
#
# tpm_clear.sh -- bring the TPM to ON, CLEARED, ENABLED from whatever state a
# previous owner, a BIOS flash, or an earlier half-finished pass left it in
# (operator 2026-10-29). Recycled blades arrive with Windows/BitLocker owning
# the TPM (et25b4 2026-10-26); post's Leopard BIOS flash can reset setup to
# "TPM State = Disabled"; post fails either.
#
# Every fix is done BY THE BIOS on the next boot, so each pass takes at most one
# step, then post.sh reboots (exit 10) and the next inventory pass verifies it
# and takes the next step. Steps, in order:
#   on      no TPM device, setup "Security Device Support" off (Leopards:
#           tpm_sce.py on)
#   enable  TPM 1.2 disabled or deactivated (tpm_sce.py enable: SCE "TPM State
#           = Enabled"; PPI op 6 answers "6 0" and changes nothing on this BIOS,
#           et29b4 2026-10-28), or TPM 2.0 storage/endorsement hierarchy off
#           (PPI op 1). A disabled 1.2 TPM is enabled BEFORE its clear.
#   clear   owned: TPM 2.0 PPI op 5; TPM 1.2 PPI op 22 (Enable + Activate +
#           Clear + Enable + Activate), else op 5, else SCE "Pending operation =
#           TPM Clear" (Leopards prompt for every PPI clear op, NoPPIClear unset).
#           A Leopard's BIOS re-disables a TPM after its clear, so a clear can be
#           followed by one more enable.
# PPI ops run only when the BIOS lists them "4: User not required" (no console
# prompt at an unattended blade).
#
# Memory between passes: one NV EFI variable per step kind, written BEFORE the
# request and holding "<how> <epoch>". A marker younger than TPM_MARKER_TTL
# (2 h) means "this boot follows our request": the step is judged (done ->
# *-AT-BOOT, marker dropped; not done -> TPM-FAIL, marker kept, never re-queued,
# so a BIOS that ignores a request costs one reboot, never a loop). An older
# marker belongs to an earlier visit: dropped, and the step may be tried again.
# The PPI response alone is NOT memory: the BIOS keeps "5 0: Success" until the
# next request, so a blade cleared last month and re-imaged since would never be
# cleared again. Only where the EFI variable store cannot be written does a PPI
# step fall back to it.
#
# Report lines on stdout (post.sh keeps them as tpmclear.txt):
#   TPM-ON-AT-BOOT sce                         the BIOS turned the TPM device on
#   TPM-ENABLED-AT-BOOT <sce|ppi response>     the BIOS ran our enable
#   TPM-CLEARED-AT-BOOT <sce|ppi response>     the BIOS ran our clear
#   TPM-CLEAN <state>                          on, unowned, enabled
#   TPM-QUEUED op=<on|sce-enable|1|sce|22|5 ...> <state>  reboot to run it (exit 10)
#   TPM-SKIP reason=<why>                      no TPM device on this blade
#   TPM-FAIL reason=<why> <state>              goal not reached; look at the node
# Always exit 0 except the queued case.

TPM="${TPM_SYSFS_DIR:-/sys/class/tpm/tpm0}"
PPI="${TPM_PPI_DIR:-$TPM/ppi}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_CMD="${TPM_STATE_CMD:-python3 $HERE/tpm_state.py}"
SCE_CMD="${TPM_SCE_CMD:-python3 $HERE/tpm_sce.py clear}"
SCE_ENABLE_CMD="${TPM_SCE_ENABLE_CMD:-python3 $HERE/tpm_sce.py enable}"
SCE_ON_CMD="${TPM_SCE_ON_CMD:-python3 $HERE/tpm_sce.py on}"
EFIVARS="${TPM_EFIVARS_DIR:-/sys/firmware/efi/efivars}"
TTL="${TPM_MARKER_TTL:-7200}"
GUID=7a1f5c2e-3b8d-4e6a-9c41-0f2d6b8e5a93
MARKER="$EFIVARS/FlaxTpmClear-$GUID"
EMARKER="$EFIVARS/FlaxTpmEnable-$GUID"
OMARKER="$EFIVARS/FlaxTpmOn-$GUID"
now=$(date +%s)
state=""

note() { declare -F ps_note >/dev/null && ps_note "$*"; return 0; }
drop() { chattr -i "$1" 2>/dev/null; rm -f "$1"; }
mark() { printf '\x07\x00\x00\x00%s %s' "$2" "$now" > "$1" 2>/dev/null; }   # $1 marker, $2 how
how() { tail -c +5 "$1" 2>/dev/null | awk '{print $1}'; }
fail() { echo "TPM-FAIL reason=$1 $state"; exit 0; }

# fresh MARKER: 0 = set by the pass just before this boot. One without an epoch
# (written before 2026-10-29) counts as fresh and is restamped, so a step it
# failed is retried after TTL instead of never.
fresh() {
    [ -e "$1" ] || return 1
    local t; t=$(tail -c +5 "$1" 2>/dev/null | awk '{print $2}')
    case "$t" in
        ''|*[!0-9]*) mark "$1" "$(how "$1")"; return 0 ;;
    esac
    [ $((now - t)) -ge 0 ] && [ $((now - t)) -lt "$TTL" ]
}
# old markers (an earlier visit's) are dropped up front
for m in "$MARKER" "$EMARKER" "$OMARKER"; do
    if [ -e "$m" ] && ! fresh "$m"; then echo "marker $(basename "$m") from an earlier visit dropped"; drop "$m"; fi
done

# One SCE write (tpm_sce.py on|enable|clear): marker first, a skip or failure
# takes it away again. SCE skips (unknown BIOS build, question not found) are
# failures here: the TPM stays short of the goal.
sce_queue() {   # $1 command (word-split), $2 marker, $3 op name for TPM-QUEUED
    mark "$2" sce || fail efivar-marker-not-writable
    out=$($1 2>&1); rc=$?
    echo "$out" | sed 's/^/sce: /'
    last=$(echo "$out" | tail -1)
    if [ $rc = 0 ]; then
        echo "TPM-QUEUED op=$3 ${last#SCE-QUEUED } $state"; exit 10
    fi
    drop "$2"
    why=$(echo "$last" | sed -n 's/.*reason=\([^ ]*\).*/\1/p')
    fail "${why:-sce-rc$rc}"
}

# ---- on: no TPM device at all ----
if [ ! -d "$TPM" ]; then
    if [ -e "$OMARKER" ]; then fail still-no-tpm-after-security-device-enable; fi
    mark "$OMARKER" sce || { echo "TPM-SKIP reason=no-tpm-device:efivar-marker-not-writable"; exit 0; }
    out=$($SCE_ON_CMD 2>&1); rc=$?
    echo "$out" | sed 's/^/sce: /'
    last=$(echo "$out" | tail -1)
    if [ $rc = 0 ]; then
        note "no TPM device -- setting Security Device Support = Enable in the BIOS setup (SCE)"
        echo "TPM-QUEUED op=on ${last#SCE-QUEUED }"; exit 10
    fi
    drop "$OMARKER"
    why=$(echo "$last" | sed -n 's/.*reason=\([^ ]*\).*/\1/p')
    # A skip (setup already on, or a BIOS whose setup we do not know): no
    # TPM module fitted, as far as we can tell -- not a fault of this blade's TPM.
    if [ $rc = 3 ]; then echo "TPM-SKIP reason=no-tpm-device:${why:-sce-skip}"; exit 0; fi
    fail "no-tpm-device:${why:-sce-rc$rc}"
fi
if [ -e "$OMARKER" ]; then
    echo "TPM-ON-AT-BOOT sce"; note "TPM device was turned on by the BIOS setup on this boot"; drop "$OMARKER"
fi

have_ppi=0
[ -d "$PPI" ] && have_ppi=1
req=$(cat "$PPI/request" 2>/dev/null)
resp=$(cat "$PPI/response" 2>/dev/null)
[ "$have_ppi" = 1 ] && echo "ppi version=$(cat "$PPI/version" 2>/dev/null) request=$req response=$resp"
resp_op=${resp%% *}; resp_rc=$(echo "$resp" | awk '{sub(/:$/, "", $2); print $2}')   # "5 0: Success" -> 5, 0
unattended() { [ "$(awk -v op="$1" '$1==op{print $2}' "$PPI/tcg_operations" 2>/dev/null)" = "4:" ]; }

# Can the EFI variable store hold a marker? Only if not does a PPI step fall
# back to the BIOS response as its memory. Probed only when that matters (an
# NVRAM write per pass for nothing otherwise).
efi_ok=""
efi_writable() {
    if [ -z "$efi_ok" ]; then
        local probe="$EFIVARS/FlaxTpmProbe-$GUID"
        if mark "$probe" probe; then efi_ok=1; drop "$probe"; else efi_ok=0; fi
    fi
    [ "$efi_ok" = 1 ]
}

note "reading TPM state"
state=$($STATE_CMD 2>&1)
echo "state $state"
case "$state" in
    owned=0*) owned=0 ;;
    owned=1*) owned=1 ;;
    *) fail unreadable ;;
esac
tpm12=0; disabled=0
case "$state" in
    *" tpm=1.2"*)
        tpm12=1
        case "$state" in *" enabled=0"*|*" active=0"*) disabled=1 ;; esac ;;
    *" shEnable=0"*|*" ehEnable=0"*) disabled=1 ;;
esac

# ours MARKER OPS: did the pass before this boot queue this step? A fresh
# marker, or -- no EFI store -- the BIOS answering one of our ops.
ours() {
    [ -e "$1" ] && return 0
    local o
    for o in $2; do
        [ "$resp_op" = "$o" ] && { efi_writable && return 1 || return 0; }
    done
    return 1
}
# what the BIOS said about a PPI step (a refusal beats "did not take")
ppi_why() {   # $1 marker, $2 our ops, $3 default reason
    local o
    for o in $2; do
        [ "$req" = "$o" ] && { echo "bios-left-the-request-pending:op$o"; return; }
    done
    if [ "$(how "$1")" != sce ]; then
        for o in $2; do
            if [ "$resp_op" = "$o" ] && [ "$resp_rc" != 0 ]; then echo "bios-refused:${resp// /_}"; return; fi
        done
    fi
    echo "$3"
}
said() { [ "$(how "$1")" = sce ] && echo sce || echo "$resp"; }

# ---- verify what the pass before this boot queued ----
if ours "$MARKER" "5 22"; then
    if [ "$owned" = 1 ]; then fail "$(ppi_why "$MARKER" "5 22" still-owned-after-clear)"; fi
    echo "TPM-CLEARED-AT-BOOT $(said "$MARKER")"; note "TPM was cleared by the BIOS on this boot"
    drop "$MARKER"
fi
if ours "$EMARKER" "1"; then
    if [ "$disabled" = 1 ]; then fail "$(ppi_why "$EMARKER" "1" still-disabled-after-enable)"; fi
    echo "TPM-ENABLED-AT-BOOT $(said "$EMARKER")"; note "TPM was enabled by the BIOS on this boot"
    drop "$EMARKER"
fi

# ---- next step ----
ppi_queue() {   # $1 op, $2 marker, $3 what (for the banner)
    mark "$2" "ppi$1" || echo "efi store not writable -- the BIOS response is this step's only memory"
    note "queueing a BIOS $3 (PPI op $1)"
    echo "$1" > "$PPI/request" 2>/dev/null
    if [ "$(cat "$PPI/request" 2>/dev/null)" != "$1" ]; then drop "$2"; fail "ppi-request-not-accepted:op$1"; fi
    echo "TPM-QUEUED op=$1 $state"; exit 10
}
ppi_try() {      # $1 op, $2 marker, $3 what, $4 state word for the failure
    [ "$have_ppi" = 1 ] || fail "$4:no-ppi"
    unattended "$1" || fail "$4:$(needs_user "$1")"
    ppi_queue "$@"
}
needs_user() { echo "op$1-needs-console-confirmation:$(awk -v op="$1" '$1==op{print $2}' "$PPI/tcg_operations" 2>/dev/null | grep . || echo unlisted)"; }

if [ "$disabled" = 1 ]; then
    if [ "$tpm12" = 1 ]; then
        note "TPM 1.2 disabled -- setting TPM State = Enabled in the BIOS setup (SCE)"
        sce_queue "$SCE_ENABLE_CMD" "$EMARKER" sce-enable
    fi
    ppi_try 1 "$EMARKER" enable disabled
fi
if [ "$owned" = 1 ]; then
    if [ "$tpm12" = 1 ]; then
        for o in 22 5; do
            if unattended "$o"; then ppi_queue "$o" "$MARKER" clear; fi
        done
        [ "$have_ppi" = 1 ] && echo "ppi $(needs_user 22) -- trying the BIOS setup (SCE)"
        note "TPM 1.2 owned by a previous OS -- scheduling a clear in the BIOS setup (SCE)"
        sce_queue "$SCE_CMD" "$MARKER" sce
    fi
    ppi_try 5 "$MARKER" clear owned
fi
echo "TPM-CLEAN $state"; exit 0
