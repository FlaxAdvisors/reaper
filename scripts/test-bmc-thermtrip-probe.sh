#!/bin/bash
# Tests for bmc-thermtrip-probe. Runs the REAL bin against a stub standing in
# for the BMC side of its one command (cat /sys/kernel/debug/gpio). Same
# FLAX_BMC_REMOTE_EXEC shape as test-bmc-blade-power-cycle.sh.
#
# Gates (spec 2026-09-11-thermtrip-tile-design §4):
#   a. both hi -> cpu0 clear, cpu1 clear
#   b. cpu0 lo -> cpu0 latched   c. cpu1 lo -> cpu1 latched   d. both lo
#   e. a line missing -> gpio_unreadable, no cpu0 key
#   f. a garbled value -> gpio_unreadable, no cpu0 key
#   g. empty output -> ssh_unreachable, no cpu0 key
#   h. executor failure -> ssh_unreachable, no cpu0 key
#   i. the bin sends exactly one remote command and it is a read
#
# Run: bash scripts/test-bmc-thermtrip-probe.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0

sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-thermtrip-probe.sh.j2" > "$work/bin"
chmod +x "$work/bin"

cat > "$work/stub" <<'STUB'
#!/bin/bash
ip="$1"; cmd="$2"
[ -n "${FIX_CMDLOG:-}" ] && printf '%s\n' "$cmd" >> "$FIX_CMDLOG"
[ "${FIX_FAIL:-no}" = yes ] && exit 255
printf '%s\n' "${FIX_GPIO:-}"
STUB
chmod +x "$work/stub"
export FLAX_BMC_REMOTE_EXEC="$work/stub"

line() { printf ' gpio-%s (%-20s|host-error-monitor  ) in  %s IRQ ACTIVE LOW ' "$1" "$2" "$3"; }
both() { printf '%s\n%s\n' "$(line 612 CPU0_THERMTRIP_LATCH "$1")" "$(line 613 CPU1_THERMTRIP_LATCH "$2")"; }

run() { "$work/bin" probe 10.0.0.1 2>/dev/null; }
check() { if printf '%s' "$3" | grep -qF -- "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $1: wanted '$2' in: $3"; fi; }
nokey() { if printf '%s' "$3" | grep -q "\"$2\""; then fail=$((fail+1)); echo "FAIL $1: '$2' must be absent in: $3"; else pass=$((pass+1)); fi; }
rc_is() { if [ "$2" -eq "$3" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $1: rc $3 != $2"; fi; }

export FIX_GPIO="$(both hi hi)"; out=$(run); rc=$?
check a-clear '{"cpu0":"clear","cpu1":"clear"}' "$out"; rc_is a-rc 0 $rc

export FIX_GPIO="$(both lo hi)"; out=$(run)
check b-cpu0 '"cpu0":"latched","cpu1":"clear"' "$out"

export FIX_GPIO="$(both hi lo)"; out=$(run)
check c-cpu1 '"cpu0":"clear","cpu1":"latched"' "$out"

export FIX_GPIO="$(both lo lo)"; out=$(run)
check d-both '"cpu0":"latched","cpu1":"latched"' "$out"

export FIX_GPIO="$(line 612 CPU0_THERMTRIP_LATCH hi)"; out=$(run); rc=$?
check e-missing '"error":"gpio_unreadable"' "$out"; nokey e-nokey cpu0 "$out"; rc_is e-rc 1 $rc

export FIX_GPIO="$(both hi xx)"; out=$(run)
check f-garbled '"error":"gpio_unreadable"' "$out"; nokey f-nokey cpu0 "$out"

export FIX_GPIO=""; out=$(run)
check g-empty '"error":"ssh_unreachable"' "$out"; nokey g-nokey cpu0 "$out"

export FIX_FAIL=yes; out=$(run); rc=$?
check h-fail '"error":"ssh_unreachable"' "$out"; nokey h-nokey cpu0 "$out"; rc_is h-rc 1 $rc
unset FIX_FAIL

export FIX_GPIO="$(both hi hi)" FIX_CMDLOG="$work/log"; : > "$FIX_CMDLOG"; run >/dev/null
n=$(wc -l < "$FIX_CMDLOG" | tr -d ' ')
if [ "$n" -eq 1 ] && grep -q 'cat /sys/kernel/debug/gpio' "$FIX_CMDLOG" && ! grep -qE 'echo|gpioset|> /sys' "$FIX_CMDLOG"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL i: commands sent: $(cat "$FIX_CMDLOG")"; fi

echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
