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
#   c. the up-wait cap is 480s in production (raised from 300s 2026-09-24), not 20s
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
# Task 3 (2026-09-27): every invocation in this suite keeps the manual claim,
# the reboot marker and bmc_fw's own claim dir under $work. Nothing here may
# touch /run/flax.
export FLAX_MANUAL_CLAIM_DIR="$work/manual" FLAX_REBOOT_DIR="$work/reboot" FLAX_CLAIM_DIR="$work/active"

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
# Task 4: FIX_IPLOG records EVERY call's target IP (before the move check
# below, so probes of a dead/bogus IP are logged too).
[ -n "${FIX_IPLOG:-}" ] && printf '%s %s\n' "$1" "$(printf '%s' "$cmd" | head -n 1)" >> "$FIX_IPLOG"
# Task 4 (2026-09-28): the BMC MOVES. Once the 12V write has been sent
# (FIX_POST_CUT exists), the BMC answers ONLY at FIX_ANSWER_IP -- every call
# to any other IP is a dead ssh (exit 255, no output). Before the cut it
# answers at the IP it was called with, as always.
if [ -n "${FIX_ANSWER_IP:-}" ] && [ -n "${FIX_POST_CUT:-}" ] && [ -e "$FIX_POST_CUT" ] && [ "$1" != "$FIX_ANSWER_IP" ]; then exit 255; fi
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
          # VERBATIM real-hardware shape (et23b4/et23b3, captured
          # 2026-09-04) -- this is the DEFAULT fixture for every thermtrip
          # case below, not a special one: a wrong parser must not be able
          # to pass by testing against a tidied-up line. The label carries
          # a "|consumer" suffix and internal padding, "in" is followed by
          # TWO spaces, and the value is trailed by IRQ/ACTIVE flags -- all
          # three are exactly what broke the first "last field" parser on
          # real hardware (it extracted "LOW", not "hi"/"lo", and failed
          # closed on every node).
          printf ' gpio-611 (BIOS_SPI_BMC_CTRL|some-other-consumer  ) out hi IRQ ACTIVE LOW\n'
          printf ' gpio-612 (CPU0_THERMTRIP_LATCH|host-error-monitor  ) in  %s IRQ ACTIVE LOW\n' "${FIX_THERM0:-hi}"
          printf ' gpio-613 (CPU1_THERMTRIP_LATCH|host-error-monitor  ) in  %s IRQ ACTIVE LOW\n' "${FIX_THERM1:-hi}"
      fi ;;
  *'i2cset'*)
      # fire-and-forget write; nothing to answer, exit code discarded.
      # Task 3: record the claim/marker state AT the moment the write is
      # sent; optionally let "another run" overwrite the manual claim.
      # Task 4: FIX_POST_CUT marks the moment the BMC leaves its old IP.
      [ -n "${FIX_POST_CUT:-}" ] && touch "$FIX_POST_CUT"
      if [ -n "${FIX_CLAIMLOG:-}" ]; then
          [ -e "$FLAX_MANUAL_CLAIM_DIR/et6b1" ] && echo "claim_seen_during_run $(cat "$FLAX_MANUAL_CLAIM_DIR/et6b1")" >> "$FIX_CLAIMLOG"
          [ -e "$FLAX_REBOOT_DIR/et6b1" ] && [ "$(stat -c %Y "$FLAX_REBOOT_DIR/et6b1")" -ge "${FIX_T0:-0}" ] && echo "marker_before_write" >> "$FIX_CLAIMLOG"
          [ -n "${FIX_CLAIM_STEAL:-}" ] && printf '%s\n' "$FIX_CLAIM_STEAL" > "$FLAX_MANUAL_CLAIM_DIR/et6b1"
      fi ;;
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

# Redfish stub for the interlock and --power-on (2026-09-24). Idle by default;
# FIX_TASKS_CODE / FIX_OLD_TASK make it busy, FIX_RF_POWER sets PowerState.
cat > "$work/rf" <<'RFSTUB'
#!/bin/bash
method="$1"; path="$2"; shift 2
[ -n "${FIX_CMDLOG:-}" ] && printf 'RF %s %s %s\n' "$method" "$path" "$*" >> "$FIX_CMDLOG"
[ -n "${FIX_RFIPLOG:-}" ] && printf '%s %s %s\n' "${FLAX_RF_IP:-}" "$method" "$path" >> "$FIX_RFIPLOG"
# Task 4: after the cut a moved BMC's Redfish answers only at FIX_ANSWER_IP.
if [ -n "${FIX_ANSWER_IP:-}" ] && [ -n "${FIX_POST_CUT:-}" ] && [ -e "$FIX_POST_CUT" ] && [ "${FLAX_RF_IP:-}" != "$FIX_ANSWER_IP" ]; then printf '\nHTTP=000'; exit 7; fi
case "$method $path" in
  "GET /redfish/v1/TaskService/Tasks")
      if [ -n "${FIX_OLD_TASK:-}" ]; then m='{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}'; else m='{"Members":[]}'; fi
      printf '%s\nHTTP=%s' "$m" "${FIX_TASKS_CODE:-200}" ;;
  "GET /redfish/v1/TaskService/Tasks/1") printf '%s\nHTTP=200' "$FIX_OLD_TASK" ;;
  "POST /redfish/v1/Systems/system/Actions/ComputerSystem.Reset") printf '\nHTTP=%s' "${FIX_RESET_CODE:-204}" ;;
  "GET /redfish/v1/Systems/system") printf '{"PowerState":"%s"}\nHTTP=200' "${FIX_RF_POWER:-On}" ;;
  *) printf '\nHTTP=404' ;;
esac
RFSTUB
chmod +x "$work/rf"

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
          FLAX_REDFISH_EXEC="$work/rf" FLAX_CYCLE_LOCK_DIR="$work" FLAX_PROGRESS_EVERY=1 \
          FLAX_POWER_ON_WAIT=1 \
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

# ------------------------------------------ the thermtrip PARSE itself -----
#
# THIS is the test that would have caught the real-hardware bug (first live
# run against et23b4, 2026-09-04): "the value is the last whitespace-
# separated field" is FALSE on real hardware -- the label carries a
# "|consumer" suffix and padding, "in"/"out" is followed by variable
# spacing, and the value is trailed by IRQ/ACTIVE flags, so a last-field
# parser extracted "LOW" (neither hi nor lo) and failed closed on EVERY
# node. Independent of the state-machine-level tests above (which now also
# use this same verbatim shape as their default fixture, but exercise it
# indirectly through the whole bin): this pins the exact sed pattern the
# bin uses, directly, against the VERBATIM lines captured live.
THERM_PATTERN='s/.*\) +(in|out) +(lo|hi).*/\2/'

# Structural pin FIRST: the bin must actually use this exact pattern, or the
# direct extraction check below would be testing a string this test made up
# rather than what ships.
if grep -qF "sed -E '$THERM_PATTERN'" "$here/bmc-blade-power-cycle.sh.j2"; then
    echo "ok   - the bin's thermtrip sed pattern matches what this test pins"; pass=$((pass+1))
else
    echo "FAIL - the bin's thermtrip sed pattern has drifted from what this test verifies"; fail=$((fail+1))
fi

got=$(printf ' gpio-612 (CPU0_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n' | sed -E "$THERM_PATTERN")
if [ "$got" = hi ]; then
    echo "ok   - thermtrip parse extracts 'hi' from the verbatim not-latched line (et23b4)"; pass=$((pass+1))
else
    echo "FAIL - thermtrip parse on the verbatim not-latched line"
    echo "       want: hi"; echo "       got:  $got"; fail=$((fail+1))
fi

got=$(printf ' gpio-613 (CPU1_THERMTRIP_LATCH|host-error-monitor  ) in  lo IRQ ACTIVE LOW\n' | sed -E "$THERM_PATTERN")
if [ "$got" = lo ]; then
    echo "ok   - thermtrip parse extracts 'lo' from the verbatim latched line (et23b3, CPU1 tripped)"; pass=$((pass+1))
else
    echo "FAIL - thermtrip parse on the verbatim latched line"
    echo "       want: lo"; echo "       got:  $got"; fail=$((fail+1))
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

# ----------------------------------------------- the interlock (2026-09-24) --

cmdlog="$work/cmdlog_jobrunning"
run_case "a live BMC update job blocks the cut (interlock_busy)" \
      '"error":"interlock_busy","reason":"job_running' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 \
      FIX_OLD_TASK='{"Id":"1","TaskState":"Running","PercentComplete":20}'
assert_no_cycled_key "$LAST_OUT" "interlock_busy (job) record carries no cycled key"
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - live job: the write was sent anyway"; fail=$((fail+1))
else
    echo "ok   - live job: the write was never sent"; pass=$((pass+1))
fi

cmdlog="$work/cmdlog_quiet"
run_case "a job that ended seconds ago blocks the cut (quiet window)" \
      '"reason":"quiet_window' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 \
      FIX_OLD_TASK="{\"Id\":\"1\",\"TaskState\":\"Completed\",\"EndTime\":\"$(date -u -d @$(( $(date +%s) - 20 )) +%Y-%m-%dT%H:%M:%S+00:00)\"}"
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - quiet window: the write was sent anyway"; fail=$((fail+1))
else
    echo "ok   - quiet window: the write was never sent"; pass=$((pass+1))
fi

cmdlog="$work/cmdlog_tasksdead"
run_case "an unreadable job list blocks the cut (fail closed)" \
      '"reason":"tasks_unreadable"' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_TASKS_CODE=503
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - unreadable jobs: the write was sent anyway"; fail=$((fail+1))
else
    echo "ok   - unreadable jobs: the write was never sent"; pass=$((pass+1))
fi

cmdlog="$work/cmdlog_locked"
exec 8>"$work/fw-update-1.2.3.4.lock"; flock -n 8
run_case "the local fw-update lock blocks the cut" \
      '"reason":"local_lock"' "$cmdlog" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
exec 8>&-
if grep -q 'i2cset' "$cmdlog"; then
    echo "FAIL - local lock: the write was sent anyway"; fail=$((fail+1))
else
    echo "ok   - local lock: the write was never sent"; pass=$((pass+1))
fi

# ------------------------------------------------ progress + --power-on -----

run_case "progress lines narrate the cycle on stderr" \
      'bmc-blade-power-cycle: [+' "" \
      FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3

run_power_on() {  # like run_case, with --power-on appended to the invocation
    local name="$1" want="$2"; shift 2
    LAST_OUT=$(env FLAX_POLL_INTERVAL=0 FLAX_DOWN_WAIT=2 BLADE_CYCLE_TIMEOUT=2 \
          FLAX_DOWN_CONFIRM_N=2 FLAX_MIN_DOWN_S=0 FLAX_REDFISH_EXEC="$work/rf" \
          FLAX_CYCLE_LOCK_DIR="$work" FLAX_POWER_ON_WAIT=1 \
          FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_IDCOUNTER="$work/idc.po.$RANDOM" \
          FIX_ALIVECOUNTER="$work/al.po.$RANDOM" FIX_CMDLOG="" "$@" \
          "$work/bin" cycle 1.2.3.4 --power-on 2>/dev/null)
    if [[ "$LAST_OUT" == *"$want"* ]]; then echo "ok   - $name"; pass=$((pass+1))
    else echo "FAIL - $name"; echo "       want: $want"; echo "       got:  $LAST_OUT"; fail=$((fail+1)); fi
}
run_power_on "--power-on that reaches On reports power_on:on" \
      '"cycled":true,"down_s":' FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_RF_POWER=On
[[ "$LAST_OUT" == *'"power_on":"on"'* ]] && { echo "ok   - power_on:on field present"; pass=$((pass+1)); } || { echo "FAIL - power_on:on missing: $LAST_OUT"; fail=$((fail+1)); }
run_power_on "--power-on where power-good never asserts: still cycled:true, power_on:failed" \
      '"power_on":"failed"' FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_RF_POWER=Off
run_power_on "--power-on rejected by the BMC reports power_on:rejected" \
      '"power_on":"rejected"' FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_RESET_CODE=500

# ------------------------------------ Task 3: --port, claim, reboot marker ---
# run_port <name> <extra-args-string> [FIX_*=val...] -- `cycle 10.0.0.9 <args>`
run_port() {
    local name="$1" args="$2"; shift 2
    export FIX_CLAIMLOG="$work/log.$name"; : > "$FIX_CLAIMLOG"
    : > "$work/cmd.$name"
    LAST_OUT=$(env FLAX_POLL_INTERVAL=0 FLAX_DOWN_WAIT=2 BLADE_CYCLE_TIMEOUT=2 \
          FLAX_DOWN_CONFIRM_N=2 FLAX_MIN_DOWN_S=0 FLAX_REDFISH_EXEC="$work/rf" \
          FLAX_CYCLE_LOCK_DIR="$work" FLAX_POWER_ON_WAIT=1 FIX_T0="$(date +%s)" \
          FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_IDCOUNTER="$work/idc.$name" \
          FIX_ALIVECOUNTER="$work/al.$name" FIX_CMDLOG="$work/cmd.$name" "$@" \
          "$work/bin" cycle 10.0.0.9 $args 2>"$work/err.$name"); LAST_RC=$?
}
t3ok()  { echo "ok   - $1"; pass=$((pass+1)); }
t3bad() { echo "FAIL - $1"; echo "       rc=$LAST_RC out=$LAST_OUT err=$(tail -n 2 "$work/err.$2" 2>/dev/null)"; fail=$((fail+1)); }
mt() { stat -c %Y "$1" 2>/dev/null; }
host_now=${HOSTNAME:-$(cat /proc/sys/kernel/hostname)}

rm -rf "$work/manual" "$work/reboot" "$work/active"
run_port p_happy "--power-on --port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_RF_POWER=On
[[ "$LAST_OUT" == *'"cycled":true'*'"power_on":"on"'* ]] && t3ok "cycle <ip> --power-on --port et6b1 -> cycled + power_on" || t3bad "--port happy path" p_happy
grep -Eq "^claim_seen_during_run [0-9]+@${host_now}\$" "$work/log.p_happy" && t3ok "manual claim held when the 12V write is sent, content <pid>@<host>" || t3bad "claim not held at the write ($(cat "$work/log.p_happy"))" p_happy
grep -q marker_before_write "$work/log.p_happy" && [ -e "$work/reboot/et6b1" ] && t3ok "reboot marker written BEFORE the 12V write (fix round 1 ruling)" || t3bad "marker placement: not present when the write was sent" p_happy
[ ! -e "$work/manual/et6b1" ] && t3ok "owned claim removed on exit" || t3bad "owned claim left behind" p_happy
[ ! -e "$work/active/et6b1" ] && t3ok "a bin never creates a bmc-fw-active claim" || t3bad "bin wrote into bmc-fw-active" p_happy

rm -rf "$work/manual" "$work/reboot"
run_port p_order "--port et6b1 --power-on" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_RF_POWER=On
[[ "$LAST_OUT" == *'"power_on":"on"'* ]] && [ -e "$work/reboot/et6b1" ] && t3ok "--port before --power-on: both honoured" || t3bad "--port/--power-on order" p_order

rm -rf "$work/manual" "$work/reboot"
run_port p_noport "" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
[[ "$LAST_OUT" == *'"cycled":true'* ]] && [ -z "$(ls -A "$work/manual" 2>/dev/null)" ] && [ -z "$(ls -A "$work/reboot" 2>/dev/null)" ] \
  && t3ok "no --port -> cycles, no claim, no marker" || t3bad "no --port wrote a claim/marker" p_noport

rm -rf "$work/manual" "$work/reboot"
run_port p_therm "--port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_THERM0=lo
[[ "$LAST_OUT" == *thermtrip_latched* ]] && [ ! -e "$work/reboot/et6b1" ] && [ ! -e "$work/manual/et6b1" ] \
  && t3ok "thermtrip refusal (no write) -> no marker, claim removed" || t3bad "marker/claim on thermtrip refusal" p_therm

rm -rf "$work/manual" "$work/reboot" "$work/active"; mkdir -p "$work/active"
printf 'bmc_fw\n' > "$work/active/et6b1"; touch -d '@1790000000' "$work/active/et6b1"
run_port p_foreign "--port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
[ "$(cat "$work/active/et6b1" 2>/dev/null)" = bmc_fw ] && [ "$(mt "$work/active/et6b1")" = 1790000000 ] \
  && t3ok "pre-existing bmc-fw-active claim untouched (content + mtime)" || t3bad "bmc_fw's claim was touched" p_foreign

rm -rf "$work/manual" "$work/reboot"
run_port p_stolen "--port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_CLAIM_STEAL=4242@otherhost
[ "$(cat "$work/manual/et6b1" 2>/dev/null)" = "4242@otherhost" ] && t3ok "manual claim holding another run's token is not removed" || t3bad "removed another run's manual claim" p_stolen

rm -rf "$work/manual" "$work/reboot"; mkdir -p "$work/reboot"; : > "$work/reboot/et6b1"; touch -d '@1790000000' "$work/reboot/et6b1"
t_before=$(date +%s)
run_port p_refresh "--port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
[ "$(mt "$work/reboot/et6b1")" -ge "$t_before" ] && t3ok "existing reboot marker's mtime refreshed by the write" || t3bad "marker mtime not refreshed" p_refresh

# fix round 1, M1: a repeated --port -- claim and marker name the SAME (last) port
rm -rf "$work/manual" "$work/reboot"
run_port p_repeat "--port et9b9 --power-on --port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
grep -q '^claim_seen_during_run ' "$work/log.p_repeat" && [ -e "$work/reboot/et6b1" ] \
   && [ "$(ls -A "$work/reboot")" = et6b1 ] && [ ! -e "$work/manual/et9b9" ] \
   && t3ok "repeated --port: claim and marker both on the LAST port" || t3bad "repeated --port split claim/marker ($(cat "$work/log.p_repeat"); reboot=$(ls -A "$work/reboot" 2>/dev/null))" p_repeat

# fix round 1, M3: a normal exit leaves no heartbeat `sleep` behind
rm -rf "$work/manual" "$work/reboot"
run_port p_hbsleep "--port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FLAX_CLAIM_HEARTBEAT_S=37.513
sleep 0.3
if [[ "$LAST_OUT" == *'"cycled":true'* ]] && ! pgrep -f 'sleep 37\.513' >/dev/null; then t3ok "normal exit: no orphaned heartbeat sleep"
else t3bad "heartbeat sleep survived a normal exit: $(pgrep -af 'sleep 37\.513')" p_hbsleep; pkill -f 'sleep 37\.513'; fi

for bad_args in "--port" "--port --power-on" "--port ../x" "--bogus" "--power-on --power-off"; do
    rm -rf "$work/manual" "$work/reboot"
    run_port p_usage "$bad_args" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
    if [ "$LAST_RC" = 2 ] && grep -q 'usage: bmc-blade-power-cycle cycle <bmc-ip> \[--power-on\] \[--port <port>\]' "$work/err.p_usage" \
       && ! grep -q i2cset "$work/cmd.p_usage" && [ -z "$(ls -A "$work/manual" 2>/dev/null)" ]; then
        t3ok "cycle <ip> $bad_args -> usage, exit 2, nothing sent, no claim"
    else t3bad "cycle <ip> $bad_args not a usage error" p_usage; fi
done

# ------------------------- Task 4: --port follows the blade to its current IP -
# The flax feed stub: called as `$FLAX_FEED_EXEC <port>`; FIX_FEED_MODE picks
# a normal answer (FIX_FEED_IP / FIX_FEED_CHASSIS), garbage, "unknown", an
# empty/failed call, or a non-IP string. Every call is logged with its port.
cat > "$work/feed" <<'EOF'
#!/bin/bash
[ -n "${FIX_FEEDLOG:-}" ] && printf '%s\n' "$1" >> "$FIX_FEEDLOG"
case "${FIX_FEED_MODE:-ok}" in
  ok)      printf '{"ip":"%s","chassis":"%s"}\n' "${FIX_FEED_IP:-10.0.0.9}" "${FIX_FEED_CHASSIS:-SNTEST1}" ;;
  garbage) printf 'not json {{{\n' ;;
  unknown) printf '{"ip":"unknown","chassis":"unknown"}\n' ;;
  noip)    printf '{"chassis":"SNTEST1"}\n' ;;
  notip)   printf '{"ip":"10.0.0.9; touch /tmp/pwn","chassis":"SNTEST1"}\n' ;;
  list)    printf '["10.0.0.50"]\n' ;;
  fail)    exit 22 ;;
esac
EOF
chmod +x "$work/feed"
MAC2=aa:bb:cc:dd:ee:02
# run_move <name> <args> [FIX_*=val...] -- run_port with the feed wired in and
# per-case cut/feed/ip logs.
run_move() {
    local name="$1" args="$2"; shift 2
    rm -f "$work/cut.$name"; : > "$work/feedlog.$name"; : > "$work/iplog.$name"; : > "$work/rfip.$name"
    run_port "$name" "$args" FLAX_FEED_EXEC="$work/feed" FIX_POST_CUT="$work/cut.$name" \
        FIX_FEEDLOG="$work/feedlog.$name" FIX_IPLOG="$work/iplog.$name" FIX_RFIPLOG="$work/rfip.$name" "$@"
}
rm -rf "$work/manual" "$work/reboot"

# 1. The BMC comes back on a NEW IP (DHCP churn, 2026-09-27 +404/+483 s):
#    followed by --port, the record names the new IP, and identity, power
#    state and the power-on all went to it.
run_move m_follow "--power-on --port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 \
    FIX_POWERSTATE=Off FIX_RF_POWER=On FIX_ANSWER_IP=10.0.0.50 FIX_FEED_IP=10.0.0.50
[[ "$LAST_OUT" == *'"cycled":true'* ]] && t3ok "follows the blade to a new IP by port -> cycled:true" || t3bad "follow: not cycled" m_follow
[[ "$LAST_OUT" == *'"ip":"10.0.0.50"'* ]] && t3ok "record names the new IP" || t3bad "follow: no \"ip\":\"10.0.0.50\" in record" m_follow
[[ "$LAST_OUT" == *'"power_on":"on"'* ]] && grep -q '^10\.0\.0\.50 POST .*ComputerSystem.Reset' "$work/rfip.m_follow" \
    && t3ok "power-on sent to the new IP" || t3bad "follow: power-on not at the new IP ($(cat "$work/rfip.m_follow"))" m_follow
grep -q '^10\.0\.0\.50 .*MAC=' "$work/iplog.m_follow" && grep -q '^10\.0\.0\.50 .*obmcutil' "$work/iplog.m_follow" \
    && t3ok "post-cycle identity + power state read at the new IP" || t3bad "follow: identity/state not at the new IP" m_follow
grep -qx et6b1 "$work/feedlog.m_follow" && t3ok "the feed is asked for the port given by --port" || t3bad "follow: feed not asked for et6b1 ($(sort -u "$work/feedlog.m_follow"))" m_follow

# 2. A DIFFERENT blade answers at the feed's new IP: identity is still the
#    gate -> identity_changed, and NO power-on is ever sent.
run_move m_swap "--power-on --port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 \
    FIX_ANSWER_IP=10.0.0.50 FIX_FEED_IP=10.0.0.50 FIX_MAC2="$MAC2" FIX_FEED_CHASSIS=SNOTHER
[[ "$LAST_OUT" == *'"error":"identity_changed"'* ]] && t3ok "a different blade at the new IP is refused (identity_changed)" || t3bad "swap: not identity_changed" m_swap
assert_no_cycled_key "$LAST_OUT" "identity_changed at the new IP carries no cycled key"
grep -q 'ComputerSystem.Reset' "$work/cmd.m_swap" && t3bad "powered on a different blade" m_swap || t3ok "no power-on on identity_changed"

# 3. Same move WITHOUT --port: the old fixed-IP behaviour, feed never asked.
run_move m_noport "--power-on" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 \
    FIX_ANSWER_IP=10.0.0.50 FIX_FEED_IP=10.0.0.50
[[ "$LAST_OUT" == *'"error":"never_returned"'* ]] && t3ok "no --port: old fixed-IP behaviour unchanged (never_returned)" || t3bad "noport: not never_returned" m_noport
[ ! -s "$work/feedlog.m_noport" ] && t3ok "no --port: the feed is never asked" || t3bad "noport: feed asked ($(cat "$work/feedlog.m_noport"))" m_noport
grep -q 'ComputerSystem.Reset' "$work/cmd.m_noport" && t3bad "noport: power-on sent after never_returned" m_noport || t3ok "no --port: no power-on after never_returned"

# 4. No move, --port given: the record names the original IP.
run_move m_stay "--port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_FEED_IP=10.0.0.9
[[ "$LAST_OUT" == *'"cycled":true'*'"ip":"10.0.0.9"'* ]] && t3ok "--port, BMC back on its own IP -> ip is the original" || t3bad "stay: record wrong" m_stay
# and without --port the record still carries the (only) IP
run_move m_stay2 "" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3
[[ "$LAST_OUT" == *'"cycled":true'*'"ip":"10.0.0.9"'* ]] && t3ok "no --port -> ip is the original" || t3bad "stay2: record wrong" m_stay2

# 5. A broken feed never breaks the wait: garbage / "unknown" / no ip / a
#    non-IP string / a JSON list / a failing call all fall back to the
#    ORIGINAL IP only. With the BMC back at its own IP -> cycled on it; with
#    the BMC moved -> never_returned (the non-IP string is never probed).
for mode in garbage unknown noip notip list fail; do
    run_move "m_bad_$mode" "--port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_FEED_MODE="$mode"
    [[ "$LAST_OUT" == *'"cycled":true'*'"ip":"10.0.0.9"'* ]] && grep -qx et6b1 "$work/feedlog.m_bad_$mode" \
        && t3ok "feed $mode: wait falls back to the original IP (cycled at 10.0.0.9)" || t3bad "feed $mode broke the wait" "m_bad_$mode"
    run_move "m_badmv_$mode" "--port et6b1" FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_FEED_MODE="$mode" \
        FIX_ANSWER_IP=10.0.0.50
    if [[ "$LAST_OUT" == *'"error":"never_returned"'* ]] && ! grep -qv '^10\.0\.0\.9 ' "$work/iplog.m_badmv_$mode"; then
        t3ok "feed $mode + moved BMC: never_returned, only the original IP probed"
    else t3bad "feed $mode + moved BMC ($(cut -d' ' -f1 "$work/iplog.m_badmv_$mode" | sort -u | tr '\n' ' '))" "m_badmv_$mode"; fi
done

if grep -q 'BLADE_CYCLE_TIMEOUT="${BLADE_CYCLE_TIMEOUT:-480}"' "$here/bmc-blade-power-cycle.sh.j2"; then
    echo "ok   - production up-wait cap is 480s"; pass=$((pass+1))
else
    echo "FAIL - production up-wait cap is not 480s"; fail=$((fail+1))
fi
if grep -n 'SSHPASS' "$here/bmc-blade-power-cycle.sh.j2" | grep -vE '^[0-9]+:\s*#' \
     | grep -vE 'export SSHPASS=|login \$RF_USER password \$SSHPASS"|sshpass -e' | grep -q .; then
    echo "FAIL - SSHPASS used outside export / netrc heredoc / sshpass -e"; fail=$((fail+1))
else
    echo "ok   - SSHPASS used only via export, the netrc heredoc and sshpass -e"; pass=$((pass+1))
fi

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


# ── the lock directory: sudo re-exec, never a fallback dir (2026-09-24) ─────
rodir="$work/ro-lockdir"; mkdir -p "$rodir"; chmod 555 "$rodir"
printf '#!/bin/bash\nexit 1\n' > "$work/nosudo"
printf '#!/bin/bash\n[ "$1" = "-n" ] && [ "$2" = "true" ] && exit 0\nprintf "%%s\\n" "$*" > "$FIX_SUDOLOG"\nexit 0\n' > "$work/fakesudo"
chmod +x "$work/nosudo" "$work/fakesudo"
if [ "$(id -u)" != 0 ]; then
    : > "$work/cmd.nosudo"
    o=$(env FIX_CMDLOG="$work/cmd.nosudo" FLAX_SUDO="$work/nosudo" FLAX_CYCLE_LOCK_DIR="$rodir" FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/stub" \
        "$work/bin" cycle 1.2.3.4 2>&1); r=$?
    if [ $r -eq 2 ] && [[ "$o" == *"cannot write the lock directory"* ]] && ! grep -qE '^(POST|RF POST)|i2cset' "$work/cmd.nosudo"; then
        echo "ok   - unwritable lock dir, no sudo -> exit 2, nothing sent"; pass=$((pass+1))
    else echo "FAIL - unwritable lock dir, no sudo (rc=$r out=$o)"; fail=$((fail+1)); fi
    export FIX_SUDOLOG="$work/sudolog"; : > "$FIX_SUDOLOG"
    env FLAX_SUDO="$work/fakesudo" FLAX_CYCLE_LOCK_DIR="$rodir" FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/stub" "$work/bin" cycle 1.2.3.4 >/dev/null 2>&1; r=$?
    if [ $r -eq 0 ] && grep -q -- "-n $work/bin cycle 1.2.3.4" "$FIX_SUDOLOG"; then
        echo "ok   - unwritable lock dir -> re-runs itself under sudo -n with the same args"; pass=$((pass+1))
    else echo "FAIL - sudo re-exec (rc=$r log=$(cat "$FIX_SUDOLOG"))"; fail=$((fail+1)); fi
else
    echo "ok   - (skipped sudo re-exec cases: running as root)"; pass=$((pass+1))
fi
chmod 755 "$rodir"

# ── F6 (fix round 2, 2026-09-27): the bin now runs as a PARENT (holds
#     fd 9, the pid callers see) that re-execs its body as a CHILD
#     (FLAX_BIN_CHILD=1, via `setpriv --pdeathsig KILL`) which never sees
#     fd 9. Round 1's F6 SIGKILL half killed the WHOLE process tree, so it
#     could never observe that round 1's CHILD (a subshell of the SAME
#     process, not a real child) survived a SIGKILL of just the bin's own
#     pid, kept the lock, and went on to send the 12 V cut anyway after the
#     caller had already given up (N2 -- the worst possible regression for
#     this bin: the pre-round-1 bin never sent a cut on a SIGKILL). These
#     SIGKILL the TOP-LEVEL PID ONLY.
lockfile6="$work/fw-update-10.0.0.9.lock"

cat > "$work/stub.ok6" <<'EOF'
#!/bin/bash
cmd="$2"
[ -n "${FIX_CMDLOG:-}" ] && printf '%s\n' "$cmd" >> "$FIX_CMDLOG"
case "$cmd" in
  *'MAC=%s'*) printf 'MAC=%s\nOS=%s\n' 'aa:bb:cc:dd:ee:09' 'flax-onetree-1.1.1' ;;
  *'/sys/kernel/debug/gpio'*)
      printf ' gpio-612 (CPU0_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n'
      printf ' gpio-613 (CPU1_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n' ;;
  *'i2cset'*) : ;;
  *) : ;;
esac
EOF
chmod +x "$work/stub.ok6"

# --- SIGTERM case (unchanged behaviour from round 1, re-verified here):
#     hang on the i2cset write itself, AFTER the interlock. ---
cat > "$work/stub.hangafter" <<'EOF'
#!/bin/bash
cmd="$2"
case "$cmd" in
  *'MAC=%s'*) printf 'MAC=%s\nOS=%s\n' 'aa:bb:cc:dd:ee:09' 'flax-onetree-1.1.1' ;;
  *'/sys/kernel/debug/gpio'*)
      printf ' gpio-612 (CPU0_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n'
      printf ' gpio-613 (CPU1_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n' ;;
  *'i2cset'*) exec sleep "$HANG_DUR" ;;
  *) : ;;
esac
EOF
chmod +x "$work/stub.hangafter"

rm -f "$lockfile6"
( HANG_DUR=71.418 FLAX_BMC_REMOTE_EXEC="$work/stub.hangafter" FLAX_CYCLE_LOCK_DIR="$work" FLAX_REDFISH_EXEC="$work/rf" \
    bash "$work/bin" cycle 10.0.0.9 >/dev/null 2>&1 ) & bpid=$!
for i in $(seq 1 100); do pgrep -f 'sleep 71\.418' >/dev/null 2>&1 && break; sleep 0.1; done
pgrep -f 'sleep 71\.418' >/dev/null 2>&1 || { echo "FAIL - F6 SIGTERM setup: the hang never started"; fail=$((fail+1)); }
t0=$(date +%s)
kill -TERM "$bpid"
wait "$bpid" 2>/dev/null
dt=$(( $(date +%s) - t0 ))
survivor=$(pgrep -f 'sleep 71\.418' 2>/dev/null)
if [ "$dt" -le 5 ] && [ -z "$survivor" ]; then
    echo "ok   - SIGTERM while hung on i2cset AFTER the interlock exits fast (${dt}s), no survivor"; pass=$((pass+1))
else
    echo "FAIL - SIGTERM post-interlock hang (dt=${dt}s survivor=$survivor)"; fail=$((fail+1))
fi
if flock -n "$lockfile6" true; then
    echo "ok   - per-BMC lock is free after SIGTERM (post-interlock hang)"; pass=$((pass+1))
else
    echo "FAIL - per-BMC lock still held after SIGTERM (post-interlock hang)"; fail=$((fail+1))
fi

# --- SIGKILL of the TOP-LEVEL PID ONLY (N2), TWO scenarios: hung on the
#     gpio (thermtrip) read, and hung on the interlock's own busy_check
#     Redfish read. Neither is inside a command wrapped with a lock this
#     bin still holds directly -- the PARENT holds fd 9 for the WHOLE life
#     of the child now (fix round 2), so both must show: the child gone
#     fast, the lock free immediately, and -- the actual N2 regression
#     check -- no i2cset EVER appearing in the stub cmdlog, checked again
#     after a delay to catch a cut sent by a surviving orphan. ---
sigkill_cycle_test() {  # sigkill_cycle_test <name> <bmc_remote_exec> <redfish_exec> <fingerprint>
    local name="$1" bmc_exec="$2" rf_exec="$3" fp; fp=$(echo "$4" | sed 's/\./\\./g')
    rm -f "$lockfile6"
    local cmdlog="$work/cmd.sk6.$name"; : > "$cmdlog"
    ( FLAX_BMC_REMOTE_EXEC="$bmc_exec" FLAX_REDFISH_EXEC="$rf_exec" FLAX_CYCLE_LOCK_DIR="$work" \
      FIX_CMDLOG="$cmdlog" \
      "$work/bin" cycle 10.0.0.9 >/dev/null 2>"$work/err.sk6.$name" ) &
    local bp=$! found=0 i
    for i in $(seq 1 100); do pgrep -f "sleep $fp" >/dev/null 2>&1 && { found=1; break; }; sleep 0.1; done
    if [ "$found" != 1 ]; then
        echo "FAIL - sigkill-cycle-$name: the hang never started"; fail=$((fail+1)); kill -9 "$bp" 2>/dev/null; return
    fi
    local childp=""
    for i in $(seq 1 50); do childp=$(pgrep -P "$bp" 2>/dev/null | head -1); [ -n "$childp" ] && break; sleep 0.1; done
    local t0; t0=$(date +%s)
    kill -9 "$bp"
    local child_gone=0
    for i in $(seq 1 20); do
        [ -n "$childp" ] && { kill -0 "$childp" 2>/dev/null || { child_gone=1; break; }; }
        [ -z "$childp" ] && { child_gone=1; break; }
        sleep 0.1
    done
    local dt=$(( $(date +%s) - t0 ))
    if [ "$child_gone" = 1 ] && [ "$dt" -le 3 ]; then
        echo "ok   - sigkill-cycle-$name: the child is gone within ${dt}s (--pdeathsig KILL)"; pass=$((pass+1))
    else
        echo "FAIL - sigkill-cycle-$name: child $childp still alive after ${dt}s"; fail=$((fail+1))
    fi
    if flock -n "$lockfile6" true; then
        echo "ok   - sigkill-cycle-$name: lock free immediately"; pass=$((pass+1))
    else
        echo "FAIL - sigkill-cycle-$name: lock still held"; fail=$((fail+1))
    fi
    if grep -q 'i2cset' "$cmdlog" 2>/dev/null; then
        echo "FAIL - sigkill-cycle-$name: i2cset sent immediately after SIGKILL (N2!)"; fail=$((fail+1))
    else
        echo "ok   - sigkill-cycle-$name: no i2cset sent immediately after SIGKILL"; pass=$((pass+1))
    fi
    sleep 7.5   # ruling: "within ~8s after the kill" -- catch a cut sent by a delayed orphan
    if grep -q 'i2cset' "$cmdlog" 2>/dev/null; then
        echo "FAIL - sigkill-cycle-$name: i2cset appeared within 8s (N2!)"; fail=$((fail+1))
    else
        echo "ok   - sigkill-cycle-$name: still no i2cset after 8s"; pass=$((pass+1))
    fi
    pkill -9 -f "sleep $fp" 2>/dev/null
}

# fix round 3, I2: these stubs must ANSWER VALIDLY once their own (short,
# well under the 8s check) hang ends, not just `exec sleep N` forever. A
# stub that never answers means a surviving orphan (a REGRESSION, e.g.
# --pdeathsig dropped) could only ever reach thermtrip_unknown or
# tasks_unreadable, never i2cset -- so the "no i2cset within 8s" assertion
# below could not fail no matter what it was testing against (measured:
# with the OLD forever-hanging stubs, dropping --pdeathsig still passed
# all four i2cset assertions). Plain `sleep N` (not `exec sleep N`) so the
# stub script itself resumes and prints the answer after the sleep --
# `exec` would replace the stub with `sleep` and never return to the
# printf lines below it.
cat > "$work/stub.gpiohang6" <<'EOF'
#!/bin/bash
cmd="$2"
[ -n "${FIX_CMDLOG:-}" ] && printf '%s\n' "$cmd" >> "$FIX_CMDLOG"
case "$cmd" in
  *'MAC=%s'*) printf 'MAC=%s\nOS=%s\n' 'aa:bb:cc:dd:ee:09' 'flax-onetree-1.1.1' ;;
  *'/sys/kernel/debug/gpio'*)
      sleep 3.641
      printf ' gpio-612 (CPU0_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n'
      printf ' gpio-613 (CPU1_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n'
      ;;
  *'i2cset'*) : ;;
  *) : ;;
esac
EOF
chmod +x "$work/stub.gpiohang6"
sigkill_cycle_test "gpio-hang" "$work/stub.gpiohang6" "$work/rf" "3.641"

cat > "$work/rf.hang6" <<'EOF'
#!/bin/bash
method="$1"; path="$2"; shift 2
[ -n "${FIX_CMDLOG:-}" ] && printf 'RF %s %s %s\n' "$method" "$path" "$*" >> "$FIX_CMDLOG"
[ -n "${FIX_RFIPLOG:-}" ] && printf '%s %s %s\n' "${FLAX_RF_IP:-}" "$method" "$path" >> "$FIX_RFIPLOG"
# Task 4: after the cut a moved BMC's Redfish answers only at FIX_ANSWER_IP.
if [ -n "${FIX_ANSWER_IP:-}" ] && [ -n "${FIX_POST_CUT:-}" ] && [ -e "$FIX_POST_CUT" ] && [ "${FLAX_RF_IP:-}" != "$FIX_ANSWER_IP" ]; then printf '\nHTTP=000'; exit 7; fi
case "$method $path" in
  "GET /redfish/v1/TaskService/Tasks")
      sleep 3.752
      printf '{"Members":[]}\nHTTP=200'
      ;;
  *) printf '\nHTTP=404' ;;
esac
EOF
chmod +x "$work/rf.hang6"
sigkill_cycle_test "busy-check-hang" "$work/stub.ok6" "$work/rf.hang6" "3.752"

# --- I1 (fix round 3): the pdeathsig ARMING race. `setpriv` only calls
#     prctl(PR_SET_PDEATHSIG) AFTER it execs into the target -- if the
#     PARENT is SIGKILLed in the narrow window between the fork and that
#     exec, the kernel never delivers the signal at all, and (without the
#     $PPID check the fix adds) the child runs to the 12V cut with no
#     lock and no parent (measured naturally: 1/550 spawns; every time
#     with a slowed setpriv). A `setpriv` shim placed earlier in PATH,
#     which sleeps briefly before exec'ing the real setpriv, stretches
#     that race window long enough to hit deterministically: SIGKILL the
#     top pid while the shim is still sleeping (i.e. strictly before
#     --pdeathsig is ever armed), then confirm the (now-orphaned,
#     never-armed) child still refuses to run unlocked.
mkdir -p "$work/faketools"
cat > "$work/faketools/setpriv" <<'EOF'
#!/bin/bash
sleep 0.371
exec /usr/bin/setpriv "$@"
EOF
chmod +x "$work/faketools/setpriv"
cat > "$work/stub.race" <<'EOF'
#!/bin/bash
cmd="$2"
[ -n "${FIX_CMDLOG:-}" ] && printf '%s\n' "$cmd" >> "$FIX_CMDLOG"
case "$cmd" in
  *'MAC=%s'*) printf 'MAC=%s\nOS=%s\n' 'aa:bb:cc:dd:ee:08' 'flax-onetree-1.1.1' ;;
  *'/sys/kernel/debug/gpio'*)
      printf ' gpio-612 (CPU0_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n'
      printf ' gpio-613 (CPU1_THERMTRIP_LATCH|host-error-monitor  ) in  hi IRQ ACTIVE LOW\n' ;;
  *'i2cset'*) : ;;
  *) : ;;
esac
EOF
chmod +x "$work/stub.race"
racelock="$work/fw-update-10.0.0.8.lock"; rm -f "$racelock"
racecmdlog="$work/cmd.race"; : > "$racecmdlog"
( PATH="$work/faketools:$PATH" FLAX_BMC_REMOTE_EXEC="$work/stub.race" FLAX_REDFISH_EXEC="$work/rf" FLAX_CYCLE_LOCK_DIR="$work" \
  FIX_CMDLOG="$racecmdlog" FLAX_DOWN_WAIT=1 BLADE_CYCLE_TIMEOUT=1 FLAX_POLL_INTERVAL=0.1 \
  "$work/bin" cycle 10.0.0.8 >/dev/null 2>"$work/err.race" ) &
racebp=$!
racefound=0
for i in $(seq 1 100); do pgrep -f 'sleep 0\.371' >/dev/null 2>&1 && { racefound=1; break; }; sleep 0.02; done
if [ "$racefound" != 1 ]; then
    echo "FAIL - I1 race setup: the setpriv shim's sleep never started"; fail=$((fail+1))
else
    kill -9 "$racebp"
    sleep 3   # the shim's 0.371s sleep, then the real setpriv/prctl (armed
              # too late, against the wrong/no parent), then -- if the
              # child were unprotected -- the whole cmd_cycle up to i2cset
    if grep -q 'i2cset' "$racecmdlog" 2>/dev/null; then
        echo "FAIL - I1: i2cset sent after a parent SIGKILL landed in the pdeathsig arming window"; fail=$((fail+1))
    else
        echo "ok   - I1: no i2cset sent when a parent SIGKILL lands in the pdeathsig arming window"; pass=$((pass+1))
    fi
    if flock -n "$racelock" true; then
        echo "ok   - I1: lock free after the race"; pass=$((pass+1))
    else
        echo "FAIL - I1: lock still held after the race"; fail=$((fail+1))
    fi
fi
pkill -9 -f 'sleep 0\.371' 2>/dev/null
pkill -9 -f 'bin cycle 10.0.0.8' 2>/dev/null

# --- N1: TERM (then KILL) sent to the CHILD directly, bypassing the
#     parent entirely (e.g. OOM, pkill, a supervisor that targets the
#     child). Round 1's `while [ "$rc" -gt 128 ]` loop spun at 100% CPU
#     forever here, because bash keeps returning the SAME saved exit
#     status for an already-reaped pid; `kill -0` on the child is what
#     actually tells "wait was interrupted, child still alive" apart from
#     "the child is truly gone" (fix round 2, N1). This bin's CHILD
#     branch installs no TERM/INT/HUP trap of its own (nothing here needs
#     cleanup the way bios-fw-update's $ART does), so -- unlike that bin --
#     a direct TERM hits DEFAULT disposition and terminates immediately
#     even while deep in a hung remote call; both cases can safely reuse
#     a long fixture hang. Each case is bounded by its own poll loop, not
#     a blocking `wait`, so a reintroduced spin fails this test instead of
#     hanging the suite. ---
kill_child_test6() {  # kill_child_test6 <signal-name> <sleep-duration> <fingerprint>
    local sig="$1" dur="$2" fp; fp=$(echo "$3" | sed 's/\./\\./g')
    cat > "$work/stub.kc6.$sig" <<EOF
#!/bin/bash
cmd="\$2"
case "\$cmd" in
  *'MAC=%s'*) printf 'MAC=%s\nOS=%s\n' 'aa:bb:cc:dd:ee:09' 'flax-onetree-1.1.1' ;;
  *'/sys/kernel/debug/gpio'*) exec sleep $dur ;;
  *'i2cset'*) : ;;
  *) : ;;
esac
EOF
    chmod +x "$work/stub.kc6.$sig"
    ( FLAX_BMC_REMOTE_EXEC="$work/stub.kc6.$sig" FLAX_CYCLE_LOCK_DIR="$work" FLAX_REDFISH_EXEC="$work/rf" \
        bash "$work/bin" cycle 10.0.0.9 >/dev/null 2>&1 ) &
    local bp=$! i childp=""
    for i in $(seq 1 100); do pgrep -f "sleep $fp" >/dev/null 2>&1 && break; sleep 0.1; done
    for i in $(seq 1 50); do childp=$(pgrep -P "$bp" 2>/dev/null | head -1); [ -n "$childp" ] && break; sleep 0.1; done
    if [ -z "$childp" ]; then echo "FAIL - kill-child6-$sig: could not find the child pid"; fail=$((fail+1)); kill -9 "$bp" 2>/dev/null; return; fi
    kill -s "$sig" "$childp"
    local t0 dt still_alive=1
    t0=$(date +%s)
    for i in $(seq 1 40); do
        kill -0 "$bp" 2>/dev/null || { still_alive=0; break; }
        sleep 0.1
    done
    dt=$(( $(date +%s) - t0 ))
    if [ "$still_alive" = 1 ]; then
        echo "FAIL - kill-child6-$sig: parent still alive after ${dt}s (N1 spin?)"; fail=$((fail+1))
        kill -9 "$bp" "$childp" 2>/dev/null
    else
        wait "$bp" 2>/dev/null; local wrc=$?
        if [ "$wrc" -ne 0 ]; then
            echo "ok   - kill-child6-$sig: parent exits within ${dt}s, non-zero status ($wrc)"; pass=$((pass+1))
        else
            echo "FAIL - kill-child6-$sig: parent exited with status 0 (unexpected)"; fail=$((fail+1))
        fi
    fi
    pkill -9 -f "sleep $fp" 2>/dev/null
}
kill_child_test6 TERM 75.863 "75.863"
kill_child_test6 KILL 76.974 "76.974"

# --- N1: a downstream reader closing stdout early must not spin the
#     parent (the CHILD dies of SIGPIPE). Process substitution keeps $!
#     tracking the bin's own pid. A normal, fast, successful cycle (the
#     suite's own happy-path fixture) so this resolves quickly either way. ---
FIX_MAC="$MAC1" FIX_OS="$OS1" FIX_DOWN_AFTER=1 FIX_UP_AFTER=3 FIX_POWERSTATE=Off \
  FLAX_BMC_REMOTE_EXEC="$work/stub" FLAX_REDFISH_EXEC="$work/rf" FLAX_CYCLE_LOCK_DIR="$work" \
  FLAX_POLL_INTERVAL=0 FLAX_DOWN_CONFIRM_N=2 FLAX_MIN_DOWN_S=0 \
  FIX_IDCOUNTER="$work/idc.closedout" FIX_ALIVECOUNTER="$work/alc.closedout" \
  "$work/bin" cycle 10.0.0.9 > >(head -c1 >/dev/null) 2>/dev/null &
bpid=$!
t0=$(date +%s)
still_alive=1
for i in $(seq 1 40); do
    kill -0 "$bpid" 2>/dev/null || { still_alive=0; break; }
    sleep 0.1
done
dt=$(( $(date +%s) - t0 ))
if [ "$still_alive" = 1 ]; then
    echo "FAIL - closed stdout: parent still alive after ${dt}s (N1 spin?)"; fail=$((fail+1))
    kill -9 "$bpid" 2>/dev/null
else
    echo "ok   - closed stdout: parent exits promptly (${dt}s), no spin"; pass=$((pass+1))
fi
wait "$bpid" 2>/dev/null

# ── Task 3: the manual claim lives and dies with the PARENT (lock holder) ────
# Hung in the busy_check Redfish read (BEFORE the write): claim held with the
# PARENT's token, heartbeat moves its mtime; TERM removes it; SIGKILL leaves
# it with its last-heartbeat mtime, never touched again.
cat > "$work/rf.hangclaim" <<'EOF'
#!/bin/bash
case "$1 $2" in
  "GET /redfish/v1/TaskService/Tasks") exec sleep "$HANG_DUR" ;;
  *) printf '\nHTTP=404' ;;
esac
EOF
chmod +x "$work/rf.hangclaim"
claim_signal_test_cycle() {  # <TERM|KILL> <sleep-duration>
    local sig="$1" dur="$2" fp; fp=$(echo "$dur" | sed 's/\./\\./g')
    rm -rf "$work/manual" "$work/reboot"; rm -f "$lockfile6"
    local cmdlog="$work/cmd.claim$sig"; : > "$cmdlog"
    local t_start; t_start=$(date +%s)
    ( HANG_DUR="$dur" FLAX_BMC_REMOTE_EXEC="$work/stub.ok6" FLAX_REDFISH_EXEC="$work/rf.hangclaim" FLAX_CYCLE_LOCK_DIR="$work" \
      FLAX_CLAIM_HEARTBEAT_S=1 FIX_CMDLOG="$cmdlog" \
      "$work/bin" cycle 10.0.0.9 --power-on --port et6b1 >/dev/null 2>"$work/err.claim$sig" ) &
    local bp=$! i
    for i in $(seq 1 100); do pgrep -f "sleep $fp" >/dev/null 2>&1 && break; sleep 0.1; done
    local tok; tok=$(cat "$work/manual/et6b1" 2>/dev/null)
    LAST_RC=x; LAST_OUT=""
    [ "$tok" = "$bp@$host_now" ] && t3ok "claim-$sig: claim held during the run with the PARENT's token" || t3bad "claim-$sig: claim='$tok' want '$bp@$host_now'" "claim$sig"
    local m1 m2; m1=$(mt "$work/manual/et6b1"); sleep 2.5; m2=$(mt "$work/manual/et6b1")
    [ -n "$m1" ] && [ -n "$m2" ] && [ "$m2" -gt "$m1" ] && t3ok "claim-$sig: heartbeat advances the claim's mtime ($m1 -> $m2)" || t3bad "claim-$sig: heartbeat did not advance ($m1 -> $m2)" "claim$sig"
    local ticker; ticker=$(for c in $(pgrep -P "$bp"); do grep -q USR1 "/proc/$c/cmdline" 2>/dev/null && echo "$c"; done)
    local t_kill; t_kill=$(date +%s)
    kill -s "$sig" "$bp"
    for i in $(seq 1 40); do kill -0 "$bp" 2>/dev/null || break; sleep 0.1; done
    wait "$bp" 2>/dev/null; local wrc=$?
    if [ "$sig" = TERM ]; then
        [ "$wrc" -eq 143 ] && [ ! -e "$work/manual/et6b1" ] && t3ok "claim-TERM: parent exits 143 and removes its claim" || t3bad "claim-TERM: rc=$wrc claim=$(cat "$work/manual/et6b1" 2>/dev/null)" claimTERM
    else
        local m3 m4; m3=$(mt "$work/manual/et6b1"); sleep 2.5; m4=$(mt "$work/manual/et6b1")
        [ "$(cat "$work/manual/et6b1" 2>/dev/null)" = "$bp@$host_now" ] && [ -n "$m3" ] && [ "$m3" -ge "$t_start" ] && [ "$m3" -le "$t_kill" ] \
            && t3ok "claim-KILL: claim left with the parent's token, mtime = last heartbeat before the kill" || t3bad "claim-KILL: claim/mtime wrong (m3=$m3 start=$t_start kill=$t_kill)" claimKILL
        [ "$m4" = "$m3" ] && t3ok "claim-KILL: mtime stops advancing once the parent is dead" || t3bad "claim-KILL: mtime still advancing ($m3 -> $m4)" claimKILL
    fi
    local left=""; for c in $ticker; do kill -0 "$c" 2>/dev/null && left="$left $c"; done
    [ -n "$ticker" ] && [ -z "$left" ] && t3ok "claim-$sig: heartbeat ticker existed and is gone with the parent" || { t3bad "claim-$sig: ticker='$ticker' survived='$left'" "claim$sig"; kill -9 $left 2>/dev/null; }
    ! grep -q i2cset "$cmdlog" && [ ! -e "$work/reboot/et6b1" ] && t3ok "claim-$sig: no write, no marker" || t3bad "claim-$sig: write/marker after $sig" "claim$sig"
    pkill -9 -f "sleep $fp" 2>/dev/null
}
claim_signal_test_cycle TERM 76.137
claim_signal_test_cycle KILL 77.241

echo; echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
