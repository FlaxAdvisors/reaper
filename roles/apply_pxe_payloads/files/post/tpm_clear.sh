#!/bin/bash
#
# tpm_clear.sh -- queue a TPM 2.0 clear for the BIOS when a previous OS left
# the TPM owned (Windows/BitLocker on recycled blades, et25b4 2026-10-26).
#
# The clear is the standard Physical Presence Interface request: write 5
# (TPM2 Clear) to /sys/class/tpm/tpm0/ppi/request, and the BIOS performs it
# with platform authority on the NEXT boot, so a lockout/owner password left
# behind does not matter. post.sh reboots the node when this queues a clear
# (exit 10), and the next inventory pass reads the BIOS's verdict back from
# ppi/response ("5 0: Success").
#
# Only when needed: an unowned TPM is left alone. Never a reboot loop: a
# request the BIOS left pending, or a clear it refused, is reported and NOT
# re-queued. Only when unattended: if the BIOS says op 5 needs a user at the
# console, nothing is queued (the node would sit at a prompt).
#
# TPM 1.2 (Quanta Leopards): their BIOS wants a console confirmation for every
# PPI clear op and has no NoPPIClear flag, so the clear is scheduled in the BIOS
# setup instead (tpm_sce.py: AMI SCE "Pending operation = TPM Clear"), which the
# BIOS runs on the next boot with no prompt. An NV EFI variable (MARKER) is set
# BEFORE that write and is the memory between passes: next pass, still owned ->
# TPM-FAIL (marker kept, never re-queued); cleared -> marker dropped. A 1.2 TPM
# left disabled/deactivated by OUR clear (SCE, or PPI op 5) is switched back on
# the same way: SCE "TPM State = Enabled" (tpm_sce.py enable) + one reboot, with
# its own marker (EMARKER) -- still disabled after it -> TPM-FAIL. Not PPI op 6:
# after the clear this BIOS syncs setup to "TPM State = Disabled" and re-applies
# it every POST, so op 6 answers "6 0: Success" and changes nothing (et29b3 +
# et29b4 2026-10-28). A blade an older pass left that way (response "6 0", still
# disabled) is healed once the same way. A TPM we did not clear is left alone.
#
# TPM 2.0 (TiogaPass, operator 2026-10-29): clear AND enable, like the Leopards.
# After our op 5 clear, a TPM whose storage or endorsement hierarchy is off
# (tpm_state.py shEnable=0/ehEnable=0, what the BIOS's "TPM disabled" leaves) gets
# PPI op 1 (Enable) + one reboot, unattended only. The BIOS's "1 <rc>" response
# is the memory between passes: still disabled after it -> TPM-FAIL, never re-queued.
#
# Report lines on stdout (post.sh keeps them as tpmclear.txt):
#   TPM-CLEARED-AT-BOOT <ppi response|sce> the BIOS ran our clear on this boot
#   TPM-ENABLED-AT-BOOT sce|<ppi response> the BIOS ran our re-enable on this boot
#   TPM-CLEAN <state>                      nothing to do
#   TPM-QUEUED op=<5|22|1|sce|sce-enable ...> <state>  queued; reboot to run it (exit 10)
#   TPM-SKIP reason=<why>
#   TPM-FAIL reason=<why> <state>          not re-queued; look at the node
# Always exit 0 except the queued case.

PPI="${TPM_PPI_DIR:-/sys/class/tpm/tpm0/ppi}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_CMD="${TPM_STATE_CMD:-python3 $HERE/tpm_state.py}"
SCE_CMD="${TPM_SCE_CMD:-python3 $HERE/tpm_sce.py clear}"
SCE_ENABLE_CMD="${TPM_SCE_ENABLE_CMD:-python3 $HERE/tpm_sce.py enable}"
EFIVARS="${TPM_EFIVARS_DIR:-/sys/firmware/efi/efivars}"
MARKER="$EFIVARS/FlaxTpmClear-7a1f5c2e-3b8d-4e6a-9c41-0f2d6b8e5a93"
EMARKER="$EFIVARS/FlaxTpmEnable-7a1f5c2e-3b8d-4e6a-9c41-0f2d6b8e5a93"

note() { declare -F ps_note >/dev/null && ps_note "$*"; return 0; }
drop() { chattr -i "$1" 2>/dev/null; rm -f "$1"; }
drop_marker() { drop "$MARKER"; }

# One SCE write (tpm_sce.py clear|enable): its marker is set BEFORE the write
# and is the memory between passes; a skip or a failure takes it away again.
sce_queue() {   # $1 command (word-split), $2 marker, $3 op name for TPM-QUEUED
    if ! printf '\x07\x00\x00\x00sce' > "$2" 2>/dev/null; then
        echo "TPM-SKIP reason=efivar-marker-not-writable $state"; exit 0
    fi
    out=$($1 2>&1); rc=$?
    echo "$out" | sed 's/^/sce: /'
    last=$(echo "$out" | tail -1)
    if [ $rc = 0 ]; then
        echo "TPM-QUEUED op=$3 ${last#SCE-QUEUED } $state"; exit 10
    fi
    drop "$2"
    why=$(echo "$last" | sed -n 's/.*reason=\([^ ]*\).*/\1/p')
    if [ $rc = 3 ]; then echo "TPM-SKIP reason=${why:-sce-skip} $state"; exit 0; fi
    echo "TPM-FAIL reason=${why:-sce-rc$rc} $state"; exit 0
}

if [ ! -d "$PPI" ]; then echo "TPM-SKIP reason=no-tpm-ppi"; exit 0; fi
req=$(cat "$PPI/request" 2>/dev/null)
resp=$(cat "$PPI/response" 2>/dev/null)
echo "ppi version=$(cat "$PPI/version" 2>/dev/null) request=$req response=$resp"

# The PPI clear ops this script ever queues: 5 (Clear) for TPM 2.0 and as the
# TPM 1.2 fallback, 22 (Enable + Activate + Clear + Enable + Activate) for TPM
# 1.2, because op 5 alone leaves a 1.2 TPM disabled and deactivated.
resp_op=${resp%% *}; resp_rc=$(echo "$resp" | awk '{sub(/:$/, "", $2); print $2}')   # "5 0: Success" -> 5, 0
just_cleared=0
case "$resp_op $resp_rc" in
    "5 0"|"22 0") echo "TPM-CLEARED-AT-BOOT $resp"; note "TPM was cleared by the BIOS on this boot"
                  [ "$resp_op" = 5 ] && just_cleared=1 ;;
esac
unattended() { [ "$(awk -v op="$1" '$1==op{print $2}' "$PPI/tcg_operations" 2>/dev/null)" = "4:" ]; }

note "reading TPM ownership"
state=$($STATE_CMD 2>&1)
echo "state $state"
case "$state" in
    owned=0*)
        if [ -e "$MARKER" ]; then
            echo "TPM-CLEARED-AT-BOOT sce"; note "TPM was cleared by the BIOS setup on this boot"
            drop_marker; just_cleared=1
        fi
        disabled=0
        case "$state" in
            *" tpm=1.2"*" enabled=0"*|*" tpm=1.2"*" active=0"*) disabled=1 ;;
        esac
        if [ -e "$EMARKER" ]; then
            if [ "$disabled" = 1 ]; then
                echo "TPM-FAIL reason=still-disabled-after-enable $state"; exit 0   # marker kept
            fi
            echo "TPM-ENABLED-AT-BOOT sce"; note "TPM was re-enabled by the BIOS setup on this boot"
            drop "$EMARKER"
        fi
        # resp_op 6: an older pass queued PPI op 6, which this BIOS ignores.
        if [ "$disabled" = 1 ] && { [ "$just_cleared" = 1 ] || [ "$resp_op" = 6 ]; }; then
            note "TPM 1.2 left disabled by the clear -- setting TPM State = Enabled in the BIOS setup (SCE)"
            sce_queue "$SCE_ENABLE_CMD" "$EMARKER" sce-enable
        fi
        # TPM 2.0 (TiogaPass): disabled = storage/endorsement hierarchy off.
        # Our clear that leaves it so is followed by PPI op 1 (Enable), once:
        # the BIOS's "1 <rc>" answer is the memory, like op 5's for the clear.
        tpm2_off=0
        case "$state" in
            *" tpm=1.2"*) ;;
            *" shEnable=0"*|*" ehEnable=0"*) tpm2_off=1 ;;
        esac
        if [ "$resp_op" = 1 ]; then
            if [ "$tpm2_off" = 1 ]; then
                echo "TPM-FAIL reason=still-disabled-after-enable:${resp// /_} $state"; exit 0
            fi
            [ "$resp_rc" = 0 ] && { echo "TPM-ENABLED-AT-BOOT $resp"; note "TPM was enabled by the BIOS on this boot"; }
        elif [ "$tpm2_off" = 1 ] && [ "$just_cleared" = 1 ]; then
            if [ "$req" = 1 ]; then
                echo "TPM-FAIL reason=bios-left-the-previous-enable-pending $state"; exit 0
            fi
            if ! unattended 1; then
                got=$(awk '$1==1{print $2}' "$PPI/tcg_operations" 2>/dev/null)
                echo "TPM-FAIL reason=disabled-after-clear-op1-needs-console-confirmation:${got:-unlisted} $state"; exit 0
            fi
            note "TPM 2.0 left disabled after the clear -- queueing a BIOS enable (PPI op 1)"
            echo 1 > "$PPI/request" 2>/dev/null
            if [ "$(cat "$PPI/request" 2>/dev/null)" != 1 ]; then
                echo "TPM-FAIL reason=ppi-enable-request-not-accepted $state"; exit 0
            fi
            echo "TPM-QUEUED op=1 $state"; exit 10
        fi
        echo "TPM-CLEAN $state"; exit 0 ;;
    owned=1*) ;;
    *) echo "TPM-SKIP reason=unreadable $state"; exit 0 ;;
esac
if [ -e "$MARKER" ]; then
    echo "TPM-FAIL reason=still-owned-after-sce-clear $state"; exit 0
fi

# Owned. Guards before queueing, so a BIOS that ignores or refuses the
# request costs one reboot at most, never a loop.
if [ "$req" = 5 ] || [ "$req" = 22 ]; then
    echo "TPM-FAIL reason=bios-left-the-previous-clear-pending $state"; exit 0
fi
case "$resp_op" in
    5|22)
        if [ "$resp_rc" = 0 ]; then
            echo "TPM-FAIL reason=still-owned-after-a-successful-clear $state"; exit 0
        fi
        echo "TPM-FAIL reason=bios-refused-clear:${resp// /_} $state"; exit 0 ;;
esac
case "$state" in
    *" tpm=1.2"*) ops="22 5" ;;
    *)            ops="5" ;;
esac
op=""
for o in $ops; do
    if unattended "$o"; then op=$o; break; fi
done
if [ -z "$op" ]; then
    first=${ops%% *}
    got=$(awk -v op="$first" '$1==op{print $2}' "$PPI/tcg_operations" 2>/dev/null)
    case "$state" in
        *" tpm=1.2"*) ;;
        *) echo "TPM-SKIP reason=op${first}-needs-console-confirmation:${got:-unlisted} $state"; exit 0 ;;
    esac
    echo "ppi op${first}-needs-console-confirmation:${got:-unlisted} -- trying the BIOS setup (SCE)"
    note "TPM 1.2 owned by a previous OS -- scheduling a clear in the BIOS setup (SCE)"
    sce_queue "$SCE_CMD" "$MARKER" sce
fi

note "TPM owned by a previous OS -- queueing a BIOS clear (PPI op $op)"
echo "$op" > "$PPI/request" 2>/dev/null
if [ "$(cat "$PPI/request" 2>/dev/null)" != "$op" ]; then
    echo "TPM-FAIL reason=ppi-request-not-accepted $state"; exit 0
fi
echo "TPM-QUEUED op=$op $state"
exit 10
