#!/bin/bash
# Tests for chip_unlock in the BMC's bios-update (source: fb-bios-update.sh).
# The function is lifted out of the script and run against a simulated chip:
# spi_xfer is replaced by a model of the status register (WREN sets WEL, WRSR
# applies only with WEL set and the chip not hardware-protected). What this
# gates:
#   a. an unprotected chip is never written
#   b. a protected chip gets exactly WREN + WRSR, QE and CR kept
#   c. a chip that will not unlock stops the flash (rc 1, error line)
#   d. an unknown part or an unreadable register is never written
# Run: bash scripts/test-bios-update-unlock.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "ok   $1"; }
bad() { fail=$((fail + 1)); echo "FAIL $1"; echo "     rc=$rc out=$(echo "$out" | tr '\n' '|') log=$(tr '\n' '|' < "$work/log")"; }

sed -n '/^chip_unlock() {$/,/^}$/p' "$here/fb-bios-update.sh" > "$work/fn"
grep -q 'spi_user_begin' "$work/fn" || { echo "FAIL could not lift chip_unlock out of the script"; exit 1; }

cat > "$work/model" <<'M'
spi_user_begin() { echo begin >> "$LOG"; }
spi_user_end()   { echo end   >> "$LOG"; }
sleep() { :; }
spi_xfer() {    # state lives in files: command substitutions run in subshells
    local sr; sr=$(cat "$ST/sr")
    set -- $1
    case "$1" in
        0x9f) echo "${FIX_ID-c2 20 19}" ;;
        0x05) [ -n "${FIX_SR_UNREADABLE:-}" ] && { echo ""; return; }; echo "$sr" ;;
        0x15) echo "${FIX_CR:-27}" ;;
        0x06) echo wren >> "$LOG"; touch "$ST/wel" ;;
        0x01) echo "wrsr $2 $3" >> "$LOG"
              if [ -e "$ST/wel" ] && [ -z "${FIX_HW_LOCKED:-}" ]; then printf '%02x\n' $(( $2 )) > "$ST/sr"; fi
              rm -f "$ST/wel" ;;
    esac
}
M
run() {  # run <initial sr> -- sets $out $rc
    export LOG="$work/log" ST="$work/st"; rm -rf "$ST"; mkdir "$ST"; : > "$LOG"; echo "$1" > "$ST/sr"
    out=$(bash -c '. "$1"; . "$2"; chip_unlock' _ "$work/model" "$work/fn" 2>&1); rc=$?
}
wrote()  { grep -q '^wren\|^wrsr' "$work/log"; }
closed() { [ "$(grep -c '^begin$' "$work/log")" = 1 ] && [ "$(grep -c '^end$' "$work/log")" = 1 ]; }

run 00
[ $rc -eq 0 ] && ! wrote && closed && echo "$out" | grep -q 'not block-protected (SR 00)' && ok "unprotected chip -> nothing written" || bad "unprotected"
run 40
[ $rc -eq 0 ] && ! wrote && ok "QE alone is not protection -> nothing written" || bad "QE only"

run bc
[ $rc -eq 0 ] && closed && [ "$(grep -c '^wren$' "$work/log")" = 1 ] && grep -q '^wrsr 0x00 0x27$' "$work/log" \
  && [ "$(cat "$work/st/sr")" = 00 ] && echo "$out" | grep -q 'bios-update: chip unlocked (SR 00)' \
  && ok "SR bc -> WREN + WRSR 00 with CR as read, unlocked" || bad "protected"
[ "$(grep -n '^wren$' "$work/log" | cut -d: -f1)" -lt "$(grep -n '^wrsr' "$work/log" | cut -d: -f1)" ] && ok "WREN is sent before WRSR" || bad "order"

run fc
[ $rc -eq 0 ] && grep -q '^wrsr 0x40 0x27$' "$work/log" && [ "$(cat "$work/st/sr")" = 40 ] && ok "QE is kept (SR fc -> 40)" || bad "QE kept"
FIX_CR=07 run bc
grep -q '^wrsr 0x00 0x07$' "$work/log" && ok "the configuration register is written back as read" || bad "CR kept"
run 04
[ $rc -eq 0 ] && grep -q '^wrsr 0x00' "$work/log" && ok "a single block-protect bit is cleared too" || bad "one BP bit"

FIX_HW_LOCKED=1 run bc
[ $rc -eq 1 ] && closed && echo "$out" | grep -q 'bios-update: error: chip is write-protected and would not unlock (SR bc); nothing written' \
  && ok "hardware-protected chip -> rc 1 and the error line" || bad "hw locked"

FIX_ID="ef 40 19" run bc
[ $rc -eq 0 ] && ! wrote && closed && ok "unknown part -> never written, flash goes on as before" || bad "unknown part"
FIX_ID="" run bc
[ $rc -eq 0 ] && ! wrote && ok "unreadable chip id -> never written" || bad "no id"
FIX_SR_UNREADABLE=1 run bc
[ $rc -eq 0 ] && ! wrote && closed && ok "unreadable status register -> never written" || bad "sr unreadable"

# the flash path calls it, before the bind, and a failure skips flashcp
a=$(grep -n 'chip_unlock || UNLOCK_FAILED=1' "$here/fb-bios-update.sh" | cut -d: -f1)
b=$(grep -n '^echo "bind spi-aspeed-smc spi driver"' "$here/fb-bios-update.sh" | cut -d: -f1)
c=$(grep -n 'UNLOCK_FAILED:-0}" = 1 \]; then' "$here/fb-bios-update.sh" | cut -d: -f1)
d=$(grep -n 'if flashcp -v' "$here/fb-bios-update.sh" | cut -d: -f1)
rc=0; out="a=$a b=$b c=$c d=$d"
[ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ] && [ -n "$c" ] && [ -n "$d" ] && [ "$c" -lt "$d" ] \
  && ok "flash path: unlock before the bind; a failed unlock is tested before flashcp" || bad "wiring"
n=$(sed -n '/"--inspect" \]; then/,/# end of --inspect/p' "$here/fb-bios-update.sh" | grep -c 'chip_unlock\|spi_xfer 0x06\|spi_xfer "0x01')
[ "$n" = 0 ] && ok "the read-only inspect mode never unlocks" || bad "inspect unlocks"

echo "---"; echo "pass=$pass fail=$fail"
[ $fail -eq 0 ]
