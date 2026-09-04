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
# Review round 2026-09-03 added a sixth: a single alive() probe can fabricate
# a false cycled:true (ssh has failure modes ICMP does not -- session
# exhaustion, this bin's own ServerAliveInterval dropping a live session).
# Two INDEPENDENT guards close it -- a consecutive-failure debounce before
# "down" is declared, and a minimum plausible down-duration before "up" is
# trusted -- plus a split between bmc_unreachable (no round trip at all) and
# identity_unavailable (a round trip that ran, but whose identity read came
# back empty), so a wrong mgmt-interface assumption can never silently read
# as "nothing needed this feature".
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
      #
      # FIX_ROUNDTRIP_DEAD=yes prints NOTHING at all -- modelling ssh never
      # getting a remote shell (bmc_unreachable), as opposed to a shell that
      # DID run but whose identity read came back empty (FIX_MAC=""/FIX_MAC2=""
      # -- identity_unavailable). The bin's own remote script always emits
      # the literal "MAC="/"OS=" prefixes once a shell runs, even with an
      # empty substitution -- these are two structurally different failures
      # and the stub must be able to produce each on its own (review finding
      # 2026-09-03).
      if [ "${FIX_ROUNDTRIP_DEAD:-no}" = yes ]; then
          :
      else
          n=0
          [ -f "$FIX_IDCOUNTER" ] && n=$(cat "$FIX_IDCOUNTER")
          n=$((n + 1))
          printf '%s' "$n" > "$FIX_IDCOUNTER"
          if [ "$n" -eq 1 ]; then
              printf 'MAC=%s\nOS=%s\n' "${FIX_MAC-}" "${FIX_OS-}"
          else
              printf 'MAC=%s\nOS=%s\n' "${FIX_MAC2-${FIX_MAC-}}" "${FIX_OS2-${FIX_OS-}}"
          fi
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

# One env-and-run helper. Extra args are FIX_*=value pairs (and, for the
# debounce/duration-floor cases, env overrides like FLAX_POLL_INTERVAL) for
# this one case. POLL_INTERVAL=0 and small DOWN_WAIT/BLADE_CYCLE_TIMEOUT caps
# mean the negative (never-transitions) cases finish in about two real
# seconds each rather than 30s/300s, while every case that DOES transition
# (via the FIX_DOWN_AFTER/FIX_UP_AFTER counters) resolves in a handful of
# fast stub invocations regardless of the cap. DOWN_CONFIRM_N=2 and
# MIN_DOWN_S=0 are the PRODUCTION debounce default and a permissive floor
# (0 -- always satisfied) respectively, so every case that does not care
# about the debounce/floor guards is unaffected by their existence.
#
# ORDER MATTERS: the defaults come FIRST and "$@" LAST, so a case's own
# FLAX_*/FIX_* overrides in "$@" win -- `env` keeps the LAST assignment of a
# repeated NAME. A case that needs a real, non-permissive MIN_DOWN_S (the
# duration-floor tests below) passes FLAX_MIN_DOWN_S=... in its own args.
#
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
    LAST_OUT=$(env FLAX_POLL_INTERVAL=0 FLAX_DOWN_WAIT=2 BLADE_CYCLE_TIMEOUT=2 \
          FLAX_DOWN_CONFIRM_N=2 FLAX_MIN_DOWN_S=0 \
          FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_IDCOUNTER="$idc" \
          FIX_ALIVECOUNTER="$alivec" FIX_CMDLOG="$cmdlog" \
          "$@" \
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
#
# FIX_UP_AFTER=3 (not 2): the debounce (DOWN_CONFIRM_N=2) needs TWO
# consecutive failed probes -- calls 1 and 2 -- before "down" is even
# declared, so the sled must stay down through call 2 and only return at
# call 3. A fixture that came back at call 2 would never satisfy the
# debounce at all (see "a single spurious probe failure" below, which is
# exactly that fixture, repurposed to prove the debounce's absence-of-effect
# case).

cmdlog="$work/cmdlog_happy"
run_case "clean cycle: the write is actually sent" '"cycled":true' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_POWERSTATE=Off
if grep -q 'i2cset' "$cmdlog"; then
    echo "ok   - happy path actually issues the i2cset write"; pass=$((pass+1))
else
    echo "FAIL - happy path never issued the i2cset write"; fail=$((fail+1))
fi

# ------------------------------------------------------------- the preflight -

# ssh never gets a remote shell at all -- FIX_ROUNDTRIP_DEAD=yes makes the
# stub print NOTHING for the identity round trip, modelling a BMC that
# truly cannot be reached (distinct from identity_unavailable below, where
# the shell runs but the identity read itself comes back empty).
cmdlog="$work/cmdlog_unreachable"
run_case "unreachable BMC at preflight reports bmc_unreachable" \
      '"error":"bmc_unreachable"' "$cmdlog" FIX_ROUNDTRIP_DEAD=yes
assert_no_cycled_key "$LAST_OUT" "bmc_unreachable record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - a write was sent on the bmc_unreachable path (bus never reachable)"; fail=$((fail+1))
else
    echo "ok   - no write sent on the bmc_unreachable path"; pass=$((pass+1))
fi

# The BMC DOES answer -- the round trip succeeds -- but the identity read
# itself comes back empty (e.g. the mgmt interface is not eth0 on this
# image). Review finding 2026-09-03: collapsing this into bmc_unreachable
# would silently disable the feature on any fleet where that assumption is
# wrong, indistinguishable from "nothing needed it". Default stub behaviour
# (FIX_ROUNDTRIP_DEAD unset) already emits the "MAC="/"OS=" prefixes with
# empty values when FIX_MAC/FIX_OS are empty, so no new stub knob is needed
# here -- only the split in the bin itself.
cmdlog="$work/cmdlog_identity_unavailable"
run_case "reachable BMC with an unreadable identity reports identity_unavailable, not bmc_unreachable" \
      '"error":"identity_unavailable"' "$cmdlog" FIX_MAC="" FIX_OS=""
assert_no_cycled_key "$LAST_OUT" "identity_unavailable record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - a write was sent with the identity read unreadable"; fail=$((fail+1))
else
    echo "ok   - no write sent with the identity read unreadable (preflight)"; pass=$((pass+1))
fi

# Same split applies to the POST-cycle identity re-check (S4.1 step 5 tail):
# alive() already proved a shell is there, so an empty mac1 means the READ
# failed, not that a different machine answered.
run_case "identity read failing on the POST-cycle check is identity_unavailable, not identity_changed" \
      '"error":"identity_unavailable"' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_MAC2="" FIX_OS2="" \
      FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
assert_no_cycled_key "$LAST_OUT" "post-check identity_unavailable record carries no cycled key"

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

# --------------------------------------------------- the debounce (gate 1) --
#
# Review finding 2026-09-03: a single un-debounced alive() sample can
# fabricate cycled:true for a BMC that never lost power -- ssh has failure
# modes ICMP does not (session exhaustion, this bin's own
# ServerAliveInterval=2/ServerAliveCountMax=1 dropping a live session after
# a stall). THIS is the fixture the old "happy path" used to be
# (FIX_DOWN_AFTER=1 FIX_UP_AFTER=2 -- exactly one failed probe, then answers
# again): bit-identical to a genuine fast recovery unless something
# distinguishes them. DOWN_CONFIRM_N=2 is that distinction: one failed probe
# never confirms "down" at all, so this must resolve as no_effect off the
# DOWN_WAIT cap, never as a reported (and unearned) success.
cmdlog="$work/cmdlog_singleglitch"
run_case "a single spurious probe failure (never actually down) reports no_effect, not cycled:true" \
      '"error":"no_effect"' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=2
assert_no_cycled_key "$LAST_OUT" "single-glitch record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "ok   - single-glitch path did issue the write (it just had no confirmed effect)"; pass=$((pass+1))
else
    echo "FAIL - single-glitch path never even issued the write"; fail=$((fail+1))
fi

# --------------------------------------------- the duration floor (gate 2) --
#
# Even a DEBOUNCED "down" (>=2 consecutive failures) can be a burst of
# unrelated ssh failures rather than a real 12V loss, if the BMC answers
# normally again moments later. These two cases use a REAL (small, non-zero)
# POLL_INTERVAL and MIN_DOWN_S so an actual down-to-up SPAN is measured in
# wall-clock time, not just stub call count -- the floor check operates on
# date +%s, so it has to be exercised with real elapsed seconds to mean
# anything. FIX_UP_AFTER=3 (2 consecutive downs -- satisfies the debounce)
# isolates the floor as the ONLY thing left to catch the false positive.
run_case "debounced down that returns FASTER than the floor is still no_effect, not cycled:true" \
      '"error":"no_effect"' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 \
      FLAX_POLL_INTERVAL=0.3 FLAX_DOWN_WAIT=5 BLADE_CYCLE_TIMEOUT=5 FLAX_MIN_DOWN_S=1
assert_no_cycled_key "$LAST_OUT" "sub-floor down-span record carries no cycled key"

# Positive control: the SAME debounce, but the sled stays down long enough
# (several more poll intervals) to clear the floor -- proving the floor does
# not also reject a genuine recovery.
run_case "debounced down that returns AFTER the floor reports cycled:true" \
      '"cycled":true' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=7 FIX_POWERSTATE=Off \
      FLAX_POLL_INTERVAL=0.3 FLAX_DOWN_WAIT=5 BLADE_CYCLE_TIMEOUT=5 FLAX_MIN_DOWN_S=1

# -------------------------------------------------------- identity_changed --

run_case "sled returns with a different MAC reports identity_changed" \
      '"error":"identity_changed"' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_MAC2=ff:ff:ff:ff:ff:ff \
      FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
assert_no_cycled_key "$LAST_OUT" "identity_changed (mac) record carries no cycled key"

run_case "sled returns with a different OS build reports identity_changed" \
      '"error":"identity_changed"' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_OS2=flax-onetree-9.9.9 \
      FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
assert_no_cycled_key "$LAST_OUT" "identity_changed (os) record carries no cycled key"

# ------------------------------------------------------------- bmc_degraded -

run_case "BMC up but power state unreadable reports bmc_degraded" \
      '"error":"bmc_degraded"' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_POWERSTATE=""
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
