#!/bin/bash
# Tests for `bios-update --inspect` (source: fb-bios-update.sh). Runs the REAL
# script with its hardware functions replaced through BIOS_UPDATE_TEST_HOOKS;
# the chip is an ordinary file. What this gates:
#   a. an answering ME (any reply, even late) never takes the bus
#   b. the bus is always handed back, with a mux readback, on error and signal
#   c. blank is decided by comparison, and broken plumbing is never "blank"
#   d. the image structure is reported only for a non-blank chip
# Run: bash scripts/test-bios-update-inspect.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0
ok()  { pass=$((pass + 1)); echo "ok   $1"; }
bad() { fail=$((fail + 1)); echo "FAIL $1"; echo "     rc=$rc out=$(echo "$out" | tr '\n' '|')"; }

# The BMC has hexdump; a dev host may not. Provide a stand-in for the one
# format the script uses ('1/1 "%02x "') so the test does not depend on it.
if ! command -v hexdump >/dev/null 2>&1; then
    mkdir -p "$work/shim"
    printf '#!/bin/sh\nod -An -v -tx1 | tr -s " \\n" "  " | sed "s/^ *//"\n' > "$work/shim/hexdump"
    chmod +x "$work/shim/hexdump"; PATH="$work/shim:$PATH"
fi

cat > "$work/hooks" <<'H'
power_status() { echo "${FIX_HOST:-off}"; }
me_state() {
    echo x >> "$FIX_LOG.me"
    n=$(wc -l < "$FIX_LOG.me")
    if [ -n "${FIX_ME_AFTER:-}" ] && [ "$n" -ge "$FIX_ME_AFTER" ]; then echo "${FIX_ME_THEN:-normal}"
    else echo "${FIX_ME-silent}"; fi          # FIX_ME="" = an unreadable state
}
sleep() { :; }
insp_take_bus() { echo take >> "$FIX_LOG"; }
insp_regs() {
    echo regs >> "$FIX_LOG"
    [ -n "${FIX_SLOW:-}" ] && command sleep "$FIX_SLOW"
    printf 'RDID   (9F): %s\nSFDP   (5A): %s\n' "${FIX_RDID:-c2 20 19}" "$FIX_SFDP"
}
insp_bind() { echo bind >> "$FIX_LOG"; [ -n "${FIX_NOBIND:-}" ] && return 1; echo "$FIX_CHIP"; }
insp_size() { stat -c %s "$1"; }
insp_release_bus() { echo release >> "$FIX_LOG"; echo "${FIX_MUX:-0}"; }
H

# SFDP: 128 bytes; the basic-table word is bytes 49-52.
sfdp() { printf '00 %.0s' $(seq 48); printf '%s ' "$1"; printf '00 %.0s' $(seq 76); }
export FIX_SFDP; FIX_SFDP=$(sfdp "e5 20 f3 ff")

SIZE=$((16 * 1024 * 1024))
ffchip() { head -c "$SIZE" /dev/zero | tr '\000' '\377' > "$1"; }
poke() { printf "$3" | dd of="$1" bs=1 seek="$2" conv=notrunc 2>/dev/null; }   # poke <file> <offset> <printf bytes>
ffchip "$work/blank"
ffchip "$work/foreign"; poke "$work/foreign" 0 '\x3f\x00\xc0\xe3\x11\x22\x33\x44\x55\x66\x77\x88\x99\xaa\xbb\xcc\x3f\x00\xc0\xe3'
ffchip "$work/bios"
poke "$work/bios" 16 '\x5a\xa5\xf0\x0f'            # descriptor signature
poke "$work/bios" 22 '\x04'                        # FLMAP0 FRBA -> 0x40
poke "$work/bios" 68 '\x00\x08\xff\x0f'            # FLREG1 (BIOS) at 0x44
poke "$work/bios" 72 '\x03\x00\xff\x07'            # FLREG2 (ME)   at 0x48, base 0x3000
poke "$work/bios" $((0x3010)) '$FPT'
ffchip "$work/mid";  poke "$work/mid" $((64 * 65536)) 'x'     # content only at 4 MiB
ffchip "$work/top";  poke "$work/top" $((SIZE - 16)) 'x'      # content only in the last block

run() {  # run <name> [extra PATH dir] -- sets $out $rc; fixtures from the environment
    export FIX_LOG="$work/log.$1"; : > "$FIX_LOG"; : > "$FIX_LOG.me"
    out=$(PATH="${2:+$2:}$PATH" BIOS_UPDATE_TEST_HOOKS="$work/hooks" bash "$here/fb-bios-update.sh" --inspect 2>/dev/null)
    rc=$?
}
fact() { printf '%s\n' "$out" | sed -n "s/^inspect: $1=//p" | head -n 1; }
has()  { printf '%s\n' "$out" | grep -q "^inspect: $1="; }
took() { grep -q '^take$' "$FIX_LOG"; }
released() { grep -c '^release$' "$FIX_LOG"; }

# ── a. the ME decides whether the bus is taken ───────────────────────────────
FIX_HOST=on run hoston
[ $rc -eq 1 ] && [ "$(fact error)" = host_not_off ] && ! took && ok "host on -> host_not_off, bus not taken" || bad "host on"

for st in normal recovery "other:57 00"; do
    FIX_ME="$st" FIX_CHIP="$work/blank" run "me.${st%%:*}"
    [ $rc -eq 0 ] && [ "$(fact me)" = "${st%%:*}" ] && [ "$(fact end)" = ok ] && ! took && ! has bus && ! has mux \
      && ok "ME answers '${st%%:*}' -> bus never taken" || bad "ME answers $st"
done

FIX_ME_AFTER=3 FIX_CHIP="$work/blank" run melate
[ $rc -eq 0 ] && [ "$(fact me)" = normal ] && ! took && ok "ME silent twice then answers -> bus never taken" || bad "ME late"

FIX_ME="" FIX_CHIP="$work/blank" run meempty
[ $rc -eq 1 ] && [ "$(fact error)" = me_unreadable ] && ! took && ok "ME state unreadable -> error, bus never taken" || bad "ME unreadable"

FIX_CHIP="$work/blank" run blank
[ "$(wc -l < "$FIX_LOG.me")" -ge 10 ] && ok "silent ME is asked for the whole 30 s window (>= 10 polls)" || bad "ME window"
took && ok "presence proof: a silent ME does take the bus" || bad "presence proof take"

# ── c/d. blank, content, structure ───────────────────────────────────────────
[ $rc -eq 0 ] && [ "$(fact blank)" = 1 ] && [ "$(fact mux)" = 0 ] && [ "$(fact end)" = ok ] \
  && [ "$(fact rdid)" = "c2 20 19" ] && [ "$(fact sfdp)" = "e5 20 f3 ff" ] && [ "$(fact size)" = "$SIZE" ] \
  && [ "$(fact blank_ref)" = ecb99e6ffea7be1e5419350f725da86b ] && ! has sig && [ "$(released)" = 1 ] \
  && ok "blank chip: facts complete, no structure keys, bus released once" || bad "blank chip"
[ "$(printf '%s\n' "$out" | grep -n '^inspect: bus=taken' | cut -d: -f1)" -lt "$(printf '%s\n' "$out" | grep -n '^inspect: rdid=' | cut -d: -f1)" ] \
  && ok "bus=taken is printed before any chip fact" || bad "bus line order"

FIX_CHIP="$work/foreign" run foreign
[ $rc -eq 0 ] && [ "$(fact blank)" = 0 ] && [ "$(fact sig)" = "3f 00 c0 e3" ] && [ -z "$(fact flreg1)" ] && [ -z "$(fact fpt)" ] \
  && ok "foreign image: not blank, signature reported, no regions" || bad "foreign"

FIX_CHIP="$work/bios" run bios
[ $rc -eq 0 ] && [ "$(fact blank)" = 0 ] && [ "$(fact sig)" = "5a a5 f0 0f" ] && [ "$(fact flreg1)" = "00 08 ff 0f" ] \
  && [ "$(fact flreg2)" = "03 00 ff 07" ] && [ "$(fact fpt)" = "24 46 50 54" ] \
  && ok "BIOS image: descriptor, both regions and \$FPT reported" || bad "bios"

FIX_CHIP="$work/mid" run mid
[ "$(fact blank)" = 0 ] && ok "content only at 4 MiB -> not blank" || bad "mid content"
FIX_CHIP="$work/top" run top
[ "$(fact blank)" = 0 ] && ok "content only in the last block -> not blank" || bad "top content"

mkdir -p "$work/badbin"; printf '#!/bin/sh\nexit 2\n' > "$work/badbin/cmp"; chmod +x "$work/badbin/cmp"
FIX_CHIP="$work/blank" run badcmp "$work/badbin"
[ "$(fact blank)" = err ] && ! has sig && [ "$(fact mux)" = 0 ] && ok "broken cmp -> blank=err, never blank" || bad "broken cmp"

FIX_SFDP=$(sfdp "e5 20 f5 ff") FIX_CHIP="$work/blank" run sfdp257
[ "$(fact sfdp)" = "e5 20 f5 ff" ] && ok "257 SFDP word relayed" || bad "sfdp 257"

# ── b. the bus is always handed back ─────────────────────────────────────────
FIX_NOBIND=1 FIX_CHIP="$work/blank" run nobind
[ $rc -eq 1 ] && [ "$(fact error)" = bind_failed ] && [ "$(fact mux)" = 0 ] && [ "$(released)" = 1 ] && ! has end \
  && ok "bind failed -> error, bus released, mux reported" || bad "bind failed"

FIX_MUX=1 FIX_CHIP="$work/blank" run muxstuck
[ "$(fact mux)" = 1 ] && ok "mux readback is reported as read (1 stays 1)" || bad "mux stuck"

export FIX_LOG="$work/log.sig"; : > "$FIX_LOG"; : > "$FIX_LOG.me"
FIX_SLOW=3 FIX_CHIP="$work/blank" BIOS_UPDATE_TEST_HOOKS="$work/hooks" \
    bash "$here/fb-bios-update.sh" --inspect > "$work/out.sig" 2>/dev/null &
pid=$!                                   # the script itself (no wrapping subshell)
for _ in $(seq 50); do grep -q '^regs$' "$FIX_LOG" 2>/dev/null && break; sleep 0.1; done
kill -TERM "$pid"; wait "$pid"; rc=$?; out=$(cat "$work/out.sig")
[ $rc -eq 143 ] && [ "$(fact error)" = signal ] && [ "$(fact mux)" = 0 ] && [ "$(released)" = 1 ] && ! has end && ! grep -q '^bind$' "$FIX_LOG" \
  && ok "TERM mid-read -> bus released once, mux reported, nothing after it" || bad "signal"

# ── static: nothing in the block writes, powers or resets ────────────────────
a=$(grep -n '"--inspect" \]; then' "$here/fb-bios-update.sh" | cut -d: -f1)
b=$(grep -n '# end of --inspect' "$here/fb-bios-update.sh" | cut -d: -f1)
rc=0; out=""
[ -n "$a" ] && [ -n "$b" ] && ! sed -n "${a},${b}p" "$here/fb-bios-update.sh" | grep -q 'flashcp\|power_on\|power_off\|ipmb_req 0x2e\|ipmb_req 0x06 0x02' \
  && ok "static: no write, power or ME reset inside the inspect block" || bad "static"
sed -n "${b:-1},\$p" "$here/fb-bios-update.sh" | grep -q flashcp && ok "presence proof: the same grep finds flashcp after the block" || bad "static presence"

echo "---"; echo "pass=$pass fail=$fail"
[ $fail -eq 0 ]
