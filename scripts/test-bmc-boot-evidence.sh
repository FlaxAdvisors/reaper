#!/bin/bash
# Tests for bmc-boot-evidence. Runs the REAL bin against a stub standing in
# for the BMC side of every command it sends (power state, boot-cycle
# count, the On request, the PS_PWROK read, the journal grep, GetPostCodes).
# Same FLAX_BMC_REMOTE_EXEC shape as test-bmc-blade-power-cycle.sh.
#
# What this suite gates (spec 2026-09-10-bios-chip-boot-evidence-design §5):
#   a. a node that is not off is refused BEFORE any On request
#   b. "power good failed to assert" in the journal wins immediately
#   c. a cycle whose only POST code is 0xFF is NOT bios_executing (spec D4)
#   d. one non-0xFF code in the NEW cycle is bios_executing, even at t=window
#   e. rails up and no non-0xFF code after the window is no_bios_executing
#   f. every error path emits no "outcome" key
#   h. the journal --since anchor is the BMC's own clock at the On request
#      (an absolute epoch), never a relative window that slides with "now"
#      and could let a stale failure line from a PREVIOUS attempt leak in
#      (review finding 2026-09-10)
#
# Run: bash scripts/test-bmc-boot-evidence.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0

sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-boot-evidence.sh.j2" > "$work/bin"
chmod +x "$work/bin"

# The stub. FIX_STATE (off|on), FIX_CYCLE (int before On), FIX_PWROK (lo|hi),
# FIX_JOURNAL (text the journal grep returns), FIX_CODES (space-separated
# decimal POST codes for the NEW cycle), FIX_ACCEPT (yes|no for the On
# request), FIX_CMDLOG (append every command sent).
#
# FIX_ON_SEEN / FIX_CYCLE_AFTER: CurrentBootCycleCount must answer
# differently before and after the On request is sent, so the bin can
# observe a NEW boot cycle. The stub tracks "has On been requested yet" with
# a marker file the RequestedHostTransition case touches; CurrentBootCycleCount
# echoes FIX_CYCLE_AFTER once that marker exists, FIX_CYCLE (or its default)
# before.
#
# FIX_NOW: the BMC-side "date +%s" answer, fixed (default 1789000000) rather
# than the real clock -- the bin only calls this ONCE per invocation now (to
# anchor the journal --since window before the On request; elapsed_s stays
# on the bin's own local clock, see bmc-boot-evidence.sh.j2), so a fixed
# answer never stalls the window-timeout cases (c/e), and case h can pin an
# exact value to assert against.
cat > "$work/stub" <<'STUB'
#!/bin/bash
ip="$1"; cmd="$2"
[ -n "${FIX_CMDLOG:-}" ] && printf '%s\n' "$cmd" >> "$FIX_CMDLOG"
case "$cmd" in
  *CurrentPowerState*)      echo "s \"xyz.openbmc_project.State.Chassis.PowerState.${FIX_STATE_WORD:-Off}\"" ;;
  *CurrentBootCycleCount*)  if [ -e "${FIX_ON_SEEN:-/tmp/on-seen}" ]; then echo "q ${FIX_CYCLE_AFTER:-${FIX_CYCLE:-12}}"; else echo "q ${FIX_CYCLE:-12}"; fi ;;
  *RequestedHostTransition*) touch "${FIX_ON_SEEN:-/tmp/on-seen}"; [ "${FIX_ACCEPT:-yes}" = yes ] && exit 0 || exit 1 ;;
  *PS_PWROK*)               echo " gpio-526 (PS_PWROK            |power-control       ) in  ${FIX_PWROK:-lo} IRQ " ;;
  *'date +%s'*)             echo "${FIX_NOW:-1789000000}" ;;
  *journalctl*)             printf '%s\n' "${FIX_JOURNAL:-}" ;;
  *GetPostCodes*)           n=0; for c in ${FIX_CODES:-}; do n=$((n+1)); done
                            printf 'a(tay) %s' "$n"; for c in ${FIX_CODES:-}; do printf ' %s 0' "$c"; done; echo ;;
  *)                        echo "stub: unhandled command: $cmd" >&2; exit 99 ;;
esac
STUB
chmod +x "$work/stub"
export FLAX_BMC_REMOTE_EXEC="$work/stub"
export FLAX_BOOT_WINDOW=4 FLAX_POLL_INTERVAL=1
export FIX_ON_SEEN="$work/on-seen"

run() { "$work/bin" check 10.0.0.1 2>/dev/null; }
check() {  # name, expected-substring, actual
    if printf '%s' "$3" | grep -q -- "$2"; then pass=$((pass+1)); else
        fail=$((fail+1)); echo "FAIL $1: wanted '$2' in: $3"; fi
}
nokey() {  # name, forbidden-key, actual
    if printf '%s' "$3" | grep -q "\"$2\""; then
        fail=$((fail+1)); echo "FAIL $1: '$2' must be absent in: $3"; else pass=$((pass+1)); fi
}

# a. not off -> host_not_off, no On request sent
rm -f "$FIX_ON_SEEN"
export FIX_STATE_WORD=On FIX_CMDLOG="$work/log.a"; : > "$FIX_CMDLOG"
out=$(run); check a-host_not_off '"error":"host_not_off"' "$out"; nokey a-no-outcome outcome "$out"
if grep -q RequestedHostTransition "$FIX_CMDLOG"; then fail=$((fail+1)); echo "FAIL a: On was requested"; else pass=$((pass+1)); fi
unset FIX_CMDLOG; export FIX_STATE_WORD=Off

# b. journal says power good failed -> power_good_failed
rm -f "$FIX_ON_SEEN"
export FIX_JOURNAL='power-control[442]: PowerControl: power supply power good failed to assert' FIX_PWROK=lo FIX_CODES=""
out=$(run); check b-pgood '"outcome":"power_good_failed"' "$out"; check b-pwrok '"pwrok_s":-1' "$out"
unset FIX_JOURNAL

# c. rails up, new cycle, only 0xFF (255) -> no_bios_executing
rm -f "$FIX_ON_SEEN"
export FIX_PWROK=hi FIX_CYCLE=12 FIX_CODES="255 255"
# the stub returns cycle 12 before AND after; the bin must read the NEW cycle as FIX_CYCLE+1 -- emulate by bumping:
export FIX_CYCLE_AFTER=13
out=$(run); check c-ff-only '"outcome":"no_bios_executing"' "$out"; check c-nonff '"nonff":0' "$out"

# d. one real code -> bios_executing
rm -f "$FIX_ON_SEEN"
export FIX_CODES="255 5 6"
out=$(run); check d-exec '"outcome":"bios_executing"' "$out"; check d-nonff '"nonff":2' "$out"

# e. rails up, no codes at all -> no_bios_executing with codes 0
rm -f "$FIX_ON_SEEN"
export FIX_CODES=""
out=$(run); check e-silent '"outcome":"no_bios_executing"' "$out"; check e-codes '"codes":0' "$out"

# f. On request rejected -> request_rejected, no outcome
rm -f "$FIX_ON_SEEN"
export FIX_ACCEPT=no
out=$(run); check f-rejected '"error":"request_rejected"' "$out"; nokey f-no-outcome outcome "$out"
unset FIX_ACCEPT

# g. unreachable -> ssh_unreachable, no outcome
rm -f "$FIX_ON_SEEN"
export FLAX_BMC_REMOTE_EXEC="$work/nonexistent-stub"
out=$(run); check g-unreach '"error":"ssh_unreachable"' "$out"; nokey g-no-outcome outcome "$out"

# h. the journal window is anchored to the BMC's own clock at the On
# request (an absolute epoch via "--since @<t0>"), not a relative window
# that slides with every poll -- review finding 2026-09-10: a relative
# window would let a stale "power good failed to assert" line from a
# PREVIOUS attempt leak into a brand-new invocation's first poll. The stub
# is not a real journal (it answers FIX_JOURNAL regardless of --since, so
# case b above is unaffected either way); the assertion here is on the
# exact argument the bin sent.
rm -f "$FIX_ON_SEEN"
export FLAX_BMC_REMOTE_EXEC="$work/stub"
export FIX_NOW=1789000000 FIX_JOURNAL='power-control[9]: PowerControl: power supply power good failed to assert (stale)' \
       FIX_PWROK=lo FIX_CODES="" FIX_CMDLOG="$work/log.h"; : > "$FIX_CMDLOG"
out=$(run)
check h-outcome '"outcome":"power_good_failed"' "$out"
if grep -qF -- '--since "@1789000000"' "$FIX_CMDLOG"; then
    pass=$((pass+1))
else
    fail=$((fail+1)); echo "FAIL h-anchor: journalctl command did not carry --since \"@1789000000\""
    echo "       cmdlog:"; cat "$FIX_CMDLOG"
fi
unset FIX_NOW FIX_JOURNAL FIX_CMDLOG

ok()  { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }

# ── watch: read-only observation of the boot in progress (operator ruling O8) ──
wrun() {  # wrun <name> -- sets $out $rc
    export FIX_CMDLOG="$work/wcmd.$1"; : > "$FIX_CMDLOG"
    out=$(FLAX_BMC_REMOTE_EXEC="$work/stub" FLAX_BOOT_WINDOW=2 FLAX_POLL_INTERVAL=0 \
          "$work/bin" watch 10.0.0.1 2>/dev/null); rc=$?
}
never_requested() { ! grep -q 'RequestedHostTransition' "$FIX_CMDLOG"; }

FIX_STATE_WORD=On FIX_PWROK=hi FIX_CODES="1 2 255" wrun w1
if [ $rc -eq 0 ] && echo "$out" | grep -q '"outcome":"bios_executing"' && never_requested; then
    ok "watch: host on with a non-FF code is bios_executing, and no power request is ever sent"
else bad "watch: host on with a non-FF code is bios_executing, and no power request is ever sent"; fi

FIX_STATE_WORD=On FIX_PWROK=hi FIX_CODES="255" wrun w2
if [ $rc -eq 0 ] && echo "$out" | grep -q '"outcome":"no_bios_executing"' && never_requested; then
    ok "watch: rails up, only 0xFF after the window is no_bios_executing"
else bad "watch: rails up, only 0xFF after the window is no_bios_executing"; fi

FIX_STATE_WORD=On FIX_PWROK=lo FIX_CODES="1 2 3" wrun w3
if [ $rc -eq 0 ] && echo "$out" | grep -q '"outcome":"power_good_failed"' && never_requested; then
    ok "watch: stale cycle codes with PWROK lo are not executing (power_good_failed)"
else bad "watch: stale cycle codes with PWROK lo are not executing (power_good_failed)"; fi

FIX_STATE_WORD=Off wrun w4
if [ $rc -eq 1 ] && [ "$out" = '{"error":"host_off"}' ] && never_requested; then
    ok "watch: a host that is off is host_off (no outcome key, no power request)"
else bad "watch: a host that is off is host_off (no outcome key, no power request)"; fi

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
