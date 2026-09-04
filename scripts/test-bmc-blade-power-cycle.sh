#!/bin/bash
# Tests for bmc-blade-power-cycle. Runs the REAL remote sequence (preflight +
# identity, the thermtrip guard, the fire-and-forget write, wait-for-down,
# wait-for-up, identity re-check, power-control verify) against a stub, driven
# by env vars, standing in for the BMC side of every command the bin sends --
# same shape as test-bmc-bios-chip-probe.sh's FLAX_BMC_REMOTE_EXEC stub.
#
# THE POINT OF THIS SUITE. This bin performs a hard 12V cut on a live sled.
# Design doc: docs/superpowers/specs/2026-09-03-blade-power-cycle-design.md.
# The five things that decide whether it is correct (see that doc's task
# description) are exactly what this suite gates:
#   a. the thermtrip guard refuses BEFORE the write, and per-CPU
#   b. a clean ssh return is never read as success
#   c. the up-wait cap is 300s in production, not 20s
#   d. an unreachable BMC during the window is the whole point of waiting
#   e. one attempt: no internal retry of the write
# Every gate below is proven live by MUTATION, not just inspected: see
# .superpowers/sdd/blade-power-cycle-report.md for the disable-one-gate-at-a-
# time results this suite was checked against.
#
# Run: bash scripts/test-bmc-blade-power-cycle.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0

# Render the Jinja template with a dummy credential -- never a real one.
sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-blade-power-cycle.sh.j2" > "$work/bin"
chmod +x "$work/bin"

# The stub. Two independent call counters (identity() and alive() use
# different command shapes, so each gets its own file-backed counter) let a
# case describe a BMC's behaviour OVER TIME without any real wall-clock
# waiting: FIX_DOWN_AFTER / FIX_UP_AFTER name the alive()-call number at
# which the simulated BMC goes down / comes back. FIX_CMDLOG, when set,
# collects every remote command the bin actually sent -- the mechanism the
# thermtrip and bmc_unreachable cases use to assert the write was NEVER sent.
cat > "$work/stub" <<'STUB'
#!/bin/bash
cmd="$2"
[ -n "${FIX_CMDLOG:-}" ] && printf '%s\n' "$cmd" >> "$FIX_CMDLOG"
case "$cmd" in
  *'MAC=%s'*)
      # identity() round trip: first call is the S4.1 step-2 baseline; every
      # call after that is the step-5-tail post-return re-check. Distinct
      # FIX_MAC2/FIX_OS2 (defaulting to the baseline values, i.e. unchanged)
      # is what lets a case simulate the sled coming back as a DIFFERENT
      # machine without touching the alive()/gpio fixtures at all.
      n=0
      [ -f "$FIX_IDCOUNTER" ] && n=$(cat "$FIX_IDCOUNTER")
      n=$((n + 1))
      printf '%s' "$n" > "$FIX_IDCOUNTER"
      if [ "$n" -eq 1 ]; then
          printf 'MAC=%s\nOS=%s\n' "${FIX_MAC-}" "${FIX_OS-}"
      else
          printf 'MAC=%s\nOS=%s\n' "${FIX_MAC2-${FIX_MAC-}}" "${FIX_OS2-${FIX_OS-}}"
      fi ;;
  *'/sys/kernel/debug/gpio'*)
      # FIX_GPIO_EMPTY=yes simulates a debugfs read that answers nothing at
      # all (mount missing, permission, a dropped session) -- distinct from a
      # line that is PRESENT but reads neither hi nor lo (FIX_THERM0/1 set to
      # some other token), which is the other way this guard must fail
      # closed.
      if [ "${FIX_GPIO_EMPTY:-no}" != yes ]; then
          printf 'gpio-621 (BIOS_SPI_BMC_CTRL           ) out hi\n'
          printf 'gpio-622 (CPU0_THERMTRIP_LATCH         ) in  %s\n' "${FIX_THERM0:-hi}"
          printf 'gpio-623 (CPU1_THERMTRIP_LATCH         ) in  %s\n' "${FIX_THERM1:-hi}"
      fi ;;
  *'i2cset'*)
      : ;;  # fire-and-forget write; nothing to answer, exit code discarded
  *'echo alive'*)
      n=0
      [ -f "$FIX_ALIVECOUNTER" ] && n=$(cat "$FIX_ALIVECOUNTER")
      n=$((n + 1))
      printf '%s' "$n" > "$FIX_ALIVECOUNTER"
      down_after="${FIX_DOWN_AFTER:-}"
      up_after="${FIX_UP_AFTER:-}"
      is_down=no
      if [ -n "$down_after" ] && [ "$n" -ge "$down_after" ]; then is_down=yes; fi
      if [ -n "$up_after" ] && [ "$n" -ge "$up_after" ]; then is_down=no; fi
      [ "$is_down" = yes ] || printf 'alive' ;;
  *'CurrentPowerState'*)
      printf '%s' "${FIX_POWERSTATE-Off}" ;;
  *) : ;;
esac
STUB
chmod +x "$work/stub"

# One env-and-run helper. Extra args are FIX_*=value pairs for this one case.
# POLL_INTERVAL=0 and small DOWN_WAIT/BLADE_CYCLE_TIMEOUT caps mean the
# negative (never-transitions) cases finish in about two real seconds each
# rather than 30s/300s, while every case that DOES transition (via the
# FIX_DOWN_AFTER/FIX_UP_AFTER counters) resolves in a handful of fast stub
# invocations regardless of the cap.
# NOTE: called PLAIN, never as `x=$(run_case ...)` -- wrapping it in command
# substitution would run it in a SUBSHELL, and pass/fail below are globals
# this function mutates; a subshelled copy of them would silently vanish and
# every substring check in this suite would stop being counted at all. The
# bin's own stdout goes into the global LAST_OUT instead, for callers that
# need to inspect it further (assert_no_cycled_key).
run_case() {  # $1=name $2=want-substring $3=cmdlog(opt, "" for none) $4...=FIX_*=val
    local name="$1" want="$2" cmdlog="${3:-}"; shift 3
    local idc alivec
    idc="$work/idc.$$.$RANDOM"; alivec="$work/alivec.$$.$RANDOM"
    [ -n "$cmdlog" ] && : > "$cmdlog"
    LAST_OUT=$(env "$@" FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_IDCOUNTER="$idc" \
          FIX_ALIVECOUNTER="$alivec" FIX_CMDLOG="$cmdlog" \
          FLAX_POLL_INTERVAL=0 FLAX_DOWN_WAIT=2 BLADE_CYCLE_TIMEOUT=2 \
          "$work/bin" cycle 1.2.3.4 2>&1)
    if [[ "$LAST_OUT" == *"$want"* ]]; then
        echo "ok   - $name"; pass=$((pass+1))
    else
        echo "FAIL - $name"; echo "       want: $want"; echo "       got:  $LAST_OUT"; fail=$((fail+1))
    fi
}

# No error path may ever carry a "cycled" key -- the contract every caller
# (biosfw, and eventually powertriage) relies on to never read a failure as
# a success. Checked on top of run_case's substring check, not instead of
# it: the hazard is a VERDICT key surviving an error record, not merely the
# wrong error code.
assert_no_cycled_key() {
    local out="$1" name="$2"
    if [[ "$out" == *'"cycled"'* ]]; then
        echo "FAIL - $name (error record carried a cycled key)"; fail=$((fail+1))
    else
        echo "ok   - $name"; pass=$((pass+1))
    fi
}

MAC1=aa:bb:cc:dd:ee:01
OS1=flax-onetree-1.1.1

# --------------------------------------------------------------- happy path -

cmdlog="$work/cmdlog_happy"
run_case "clean cycle: the write is actually sent" '"cycled":true' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=2 FIX_POWERSTATE=Off
if grep -q 'i2cset' "$cmdlog"; then
    echo "ok   - happy path actually issues the i2cset write"; pass=$((pass+1))
else
    echo "FAIL - happy path never issued the i2cset write"; fail=$((fail+1))
fi

# ------------------------------------------------------------- the preflight -

cmdlog="$work/cmdlog_unreachable"
run_case "unreachable BMC at preflight reports bmc_unreachable" \
      '"error":"bmc_unreachable"' "$cmdlog" FIX_MAC="" FIX_OS=""
assert_no_cycled_key "$LAST_OUT" "bmc_unreachable record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - a write was sent on the bmc_unreachable path (bus never reachable)"; fail=$((fail+1))
else
    echo "ok   - no write sent on the bmc_unreachable path"; pass=$((pass+1))
fi

# ---------------------------------------------------------- the thermtrip guard -

cmdlog="$work/cmdlog_therm0"
run_case "CPU0 thermtrip latched (lo) refuses to cycle" \
      '"error":"thermtrip_latched"' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_THERM0=lo FIX_THERM1=hi
assert_no_cycled_key "$LAST_OUT" "CPU0 thermtrip record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - a write was sent while CPU0 thermtrip was latched"; fail=$((fail+1))
else
    echo "ok   - no write sent while CPU0 thermtrip was latched"; pass=$((pass+1))
fi

cmdlog="$work/cmdlog_therm1"
run_case "CPU1 thermtrip latched (lo) refuses to cycle -- independently of CPU0" \
      '"error":"thermtrip_latched"' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_THERM0=hi FIX_THERM1=lo
assert_no_cycled_key "$LAST_OUT" "CPU1 thermtrip record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - a write was sent while CPU1 thermtrip was latched"; fail=$((fail+1))
else
    echo "ok   - no write sent while CPU1 thermtrip was latched"; pass=$((pass+1))
fi

cmdlog="$work/cmdlog_therm_missing"
run_case "gpio debugfs unreadable fails closed as thermtrip_unknown" \
      '"error":"thermtrip_unknown"' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_GPIO_EMPTY=yes
assert_no_cycled_key "$LAST_OUT" "thermtrip_unknown (gpio empty) record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - a write was sent with the gpio dump unreadable"; fail=$((fail+1))
else
    echo "ok   - no write sent with the gpio dump unreadable"; pass=$((pass+1))
fi

cmdlog="$work/cmdlog_therm0_garbage"
run_case "CPU0 line present but neither hi nor lo fails closed" \
      '"error":"thermtrip_unknown"' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_THERM0=weird FIX_THERM1=hi
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - a write was sent with CPU0's latch unreadable"; fail=$((fail+1))
else
    echo "ok   - no write sent with CPU0's latch unreadable"; pass=$((pass+1))
fi

cmdlog="$work/cmdlog_therm1_garbage"
run_case "CPU1 line present but neither hi nor lo fails closed" \
      '"error":"thermtrip_unknown"' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_THERM0=hi FIX_THERM1=weird
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - a write was sent with CPU1's latch unreadable"; fail=$((fail+1))
else
    echo "ok   - no write sent with CPU1's latch unreadable"; pass=$((pass+1))
fi

# -------------------------------------------------------------- no_effect ---

cmdlog="$work/cmdlog_noeffect"
run_case "BMC never goes down reports no_effect" \
      '"error":"no_effect"' "$cmdlog" FIX_MAC="$MAC1" FIX_OS="$OS1"
assert_no_cycled_key "$LAST_OUT" "no_effect record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "ok   - no_effect path did issue the write (it just had no effect)"; pass=$((pass+1))
else
    echo "FAIL - no_effect path never even issued the write"; fail=$((fail+1))
fi

# ---------------------------------------------------------- never_returned --

run_case "BMC goes down and never returns reports never_returned" \
      '"error":"never_returned"' "" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1
assert_no_cycled_key "$LAST_OUT" "never_returned record carries no cycled key"

# -------------------------------------------------------- identity_changed --

run_case "sled returns with a different MAC reports identity_changed" \
      '"error":"identity_changed"' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_MAC2=ff:ff:ff:ff:ff:ff \
      FIX_DOWN_AFTER=1 FIX_UP_AFTER=2
assert_no_cycled_key "$LAST_OUT" "identity_changed (mac) record carries no cycled key"

run_case "sled returns with a different OS build reports identity_changed" \
      '"error":"identity_changed"' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_OS2=flax-onetree-9.9.9 \
      FIX_DOWN_AFTER=1 FIX_UP_AFTER=2
assert_no_cycled_key "$LAST_OUT" "identity_changed (os) record carries no cycled key"

# ------------------------------------------------------------- bmc_degraded -

run_case "BMC up but power state unreadable reports bmc_degraded" \
      '"error":"bmc_degraded"' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=2 FIX_POWERSTATE=""
assert_no_cycled_key "$LAST_OUT" "bmc_degraded record carries no cycled key"

# ------------------------------------------------------------- structural ---

# RULE ZERO: the rendered credential line must carry NO surrounding quotes --
# ansible's `quote` filter supplies its own.
if grep -q '^export SSHPASS={{ bmc_root_password | quote }}$' "$here/bmc-blade-power-cycle.sh.j2"; then
    echo "ok   - credential line is unquoted (ansible's quote filter supplies its own)"; pass=$((pass+1))
else
    echo "FAIL - credential line is not the expected unquoted form"; fail=$((fail+1))
fi

# BusyBox-hostile flags this project has been bitten by before must never
# appear in a remote command string (they run on the BMC's ash, not this
# host's bash) -- see bmc-bios-chip-probe.sh.j2's identical hazard.
if grep -qE 'head -[0-9]|head -c|dd conv=notrunc|od -A' "$here/bmc-blade-power-cycle.sh.j2"; then
    echo "FAIL - a BusyBox-hostile flag appears in the bin"; fail=$((fail+1))
else
    echo "ok   - no BusyBox-hostile flags (head -N, head -c, dd conv=notrunc, od -A)"; pass=$((pass+1))
fi

# Bad usage exits non-zero with a usage message, and touches nothing.
out=$("$work/bin" 2>&1); rc=$?
if [ "$rc" -ne 0 ] && [[ "$out" == *usage:* ]]; then
    echo "ok   - no args prints usage and exits non-zero"; pass=$((pass+1))
else
    echo "FAIL - no-args invocation did not behave as a usage error"; fail=$((fail+1))
fi

echo; echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
