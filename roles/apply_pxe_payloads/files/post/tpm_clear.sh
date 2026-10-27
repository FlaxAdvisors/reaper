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
# Report lines on stdout (post.sh keeps them as tpmclear.txt):
#   TPM-CLEARED-AT-BOOT <ppi response>     the BIOS ran our clear on this boot
#   TPM-CLEAN <state>                      nothing to do
#   TPM-QUEUED <state>                     clear requested; reboot to run it   (exit 10)
#   TPM-SKIP reason=<why>
#   TPM-FAIL reason=<why> <state>          not re-queued; look at the node
# Always exit 0 except the queued case.

PPI="${TPM_PPI_DIR:-/sys/class/tpm/tpm0/ppi}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_CMD="${TPM_STATE_CMD:-python3 $HERE/tpm_state.py}"

note() { declare -F ps_note >/dev/null && ps_note "$*"; return 0; }

if [ ! -d "$PPI" ]; then echo "TPM-SKIP reason=no-tpm-ppi"; exit 0; fi
req=$(cat "$PPI/request" 2>/dev/null)
resp=$(cat "$PPI/response" 2>/dev/null)
echo "ppi version=$(cat "$PPI/version" 2>/dev/null) request=$req response=$resp"

# The PPI clear ops this script ever queues: 5 (Clear) for TPM 2.0 and as the
# TPM 1.2 fallback, 22 (Enable + Activate + Clear + Enable + Activate) for TPM
# 1.2, because op 5 alone leaves a 1.2 TPM disabled and deactivated.
resp_op=${resp%% *}; resp_rc=$(echo "$resp" | awk '{sub(/:$/, "", $2); print $2}')   # "5 0: Success" -> 5, 0
case "$resp_op $resp_rc" in
    "5 0"|"22 0") echo "TPM-CLEARED-AT-BOOT $resp"; note "TPM was cleared by the BIOS on this boot" ;;
esac

note "reading TPM ownership"
state=$($STATE_CMD 2>&1)
echo "state $state"
case "$state" in
    owned=0*) echo "TPM-CLEAN $state"; exit 0 ;;
    owned=1*) ;;
    *) echo "TPM-SKIP reason=unreadable $state"; exit 0 ;;
esac

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
unattended() { [ "$(awk -v op="$1" '$1==op{print $2}' "$PPI/tcg_operations" 2>/dev/null)" = "4:" ]; }
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
    echo "TPM-SKIP reason=op${first}-needs-console-confirmation:${got:-unlisted} $state"; exit 0
fi

note "TPM owned by a previous OS -- queueing a BIOS clear (PPI op $op)"
echo "$op" > "$PPI/request" 2>/dev/null
if [ "$(cat "$PPI/request" 2>/dev/null)" != "$op" ]; then
    echo "TPM-FAIL reason=ppi-request-not-accepted $state"; exit 0
fi
echo "TPM-QUEUED op=$op $state"
exit 10
