#!/bin/bash
# Tests for bmc-bios-chip-probe. Runs the REAL blankness pipeline (dd | tr |
# wc) against fixture chips; a single stub, driven by env vars, stands in for
# the BMC side of every command the bin sends -- power state, mux/bind
# bookkeeping, MTD discovery, size, and the two 64 KiB read windows. This is
# the same shape as test-bmc-backup-flash.sh's FLAX_BMC_REMOTE_EXEC stub: one
# stub script, rewired per case with env vars and eval, not a fresh heredoc
# per case.
#
# Run: bash scripts/test-bmc-bios-chip-probe.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0

# Render the Jinja template with a dummy credential -- never a real one.
# NOTE: '|' is not special inside a '/'-delimited sed BRE, so the pattern
# below (which itself contains '|') works unescaped -- unlike a '|'-delimited
# sed, which would collide with it.
sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-bios-chip-probe.sh.j2" > "$work/bin"
chmod +x "$work/bin"

# The stub. FIX_STATE/FIX_PNOR/FIX_MTD/FIX_SIZE/FIX_MUX/FIX_CHIP/FIX_READ_FAIL/
# FIX_RO describe one scenario; the stub answers each command shape the bin
# sends. FIX_CMDLOG, when set, gets every command the bin sends appended to
# it, so a case can assert a particular remote call was (or was not) actually
# made -- in particular, that the mux-restore command was sent (review
# finding: Important 5 asked for this, since the previous test only asserted
# on the bin's OWN local fabrication of a readback, never on whether the
# restore command reached the wire) -- and, now, which device node a read
# pipeline actually targeted (mtd<N>ro vs mtd<N>).
#
# FIX_MUX_REAL=yes is the one exception to "answer, don't execute": it makes
# the gpio/unexport case run the REAL remote restore_bus body (the same way
# the dd|tr|wc read pipeline already runs for real against a fixture), so
# that command's own internal branching gets exercised, not just simulated
# via FIX_MUX (review finding, minor -- a mutant that reverted the remote
# script's "else echo absent" back to "else echo 0" was undetected by any
# test, because every test answers FIX_MUX directly). On THIS dev host,
# /sys/class/gpio does not exist at all, so the real script's own "absent"
# branch fires deterministically and safely (verified separately: no writes
# are attempted, nothing errors beyond what is already redirected away).
cat > "$work/stub" <<'STUB'
#!/bin/bash
cmd="$2"
[ -n "${FIX_CMDLOG:-}" ] && printf '%s\n' "$cmd" >> "$FIX_CMDLOG"
case "$cmd" in
  *CurrentPowerState*)
      # Empty output here is exactly what a dropped/unreachable ssh session
      # produces -- FIX_STATE="" simulates ssh_unreachable.
      echo "$FIX_STATE" ;;
  *'= pnor'*)
      [ "$FIX_PNOR" = yes ] && echo "$FIX_MTD" ;;
  *'/size'*)
      echo "$FIX_SIZE" ;;
  *'] && echo yes'*)
      # ro-node preference check: "[ -e /dev/mtd<N>ro ] && echo yes".
      # FIX_RO=yes simulates a BMC image that exposes the ro twin (confirmed
      # live on 172.17.10.101); FIX_RO=no/unset simulates one that doesn't --
      # the bin must fall back to the plain node in that case, never fail.
      [ "${FIX_RO:-no}" = yes ] && echo yes ;;
  *'gpio/unexport'*)
      # restore_bus: unbind + gpio value 0 + readback + unexport, all in ONE
      # remote call (Critical 1 fix -- the readback must happen before the
      # gpio directory is removed, in the same round trip). FIX_MUX is the
      # value the pin reads back as; "absent" simulates a readback that
      # cannot be trusted -- the fixed bin must treat that as mux_stuck,
      # never silently as PCH (the exact bug being regression-guarded here).
      if [ "${FIX_MUX_REAL:-no}" = yes ]; then
          echo "$cmd" | bash
      else
          echo "$FIX_MUX"
      fi ;;
  *'dd if=/dev/'*)
      if [ "${FIX_READ_FAIL:-no}" = yes ]; then
          # A real ssh drop or EIO mid-pipeline lands here as unreadable,
          # non-numeric stdout -- never a clean byte count.
          echo "dd: read error: Input/output error"
      else
          # Match /dev/mtd<N> with an OPTIONAL "ro" suffix as ONE token, not
          # just the "/dev/mtd5" prefix -- a plain substring match would also
          # hit inside "/dev/mtd5ro" and leave a stray "ro" tacked onto the
          # fixture path. NOTE: "(ro)?" -- NOT "ro?", which only makes the
          # trailing "o" optional and requires a literal "r", so it silently
          # fails to match a plain (non-ro) path at all.
          echo "$cmd" | sed -E "s#/dev/mtd[0-9]+(ro)?#$FIX_CHIP#" | bash
      fi ;;
  *)
      # take_bus's bind sequence: fire-and-forget, output discarded by the
      # bin. Never executed for real -- this stub must never touch this test
      # host's own /sys/class/gpio (review's noted concern), and the bin
      # doesn't need it to for a correct verdict.
      : ;;
esac
STUB
chmod +x "$work/stub"

# --- fixture chips. 32 MiB is too big for a test; use 8 blocks of 64 KiB
# --- (512 KiB) and override the block count via FLAX_PROBE_BLOCKS, while the
# --- reported /size is a REAL plausible value (16 MiB, MIN_SIZE) so the
# --- size-plausibility check -- which runs unconditionally -- passes. Only
# --- the implausible-size case below reports a size below MIN_SIZE.
mk_blank()    { dd if=/dev/zero bs=64k count=8 2>/dev/null | tr '\0' '\377' > "$1"; }
mk_pop()      { mk_blank "$1"; printf 'BIOSHEAD' | dd of="$1" conv=notrunc 2>/dev/null
                printf 'RESETVEC' | dd of="$1" bs=64k seek=7 conv=notrunc 2>/dev/null; }
mk_bootblank(){ mk_blank "$1"; printf 'BIOSHEAD' | dd of="$1" conv=notrunc 2>/dev/null; }

mk_short() { dd if=/dev/zero bs=32k count=1 2>/dev/null | tr '\0' '\377' > "$1"; }  # 32768 of 65536 bytes: a short read
mk_zero()  { : > "$1"; }                                                              # 0 bytes: dd reads nothing at all

mk_pop "$work/chip_pop"; mk_blank "$work/chip_blank"; mk_bootblank "$work/chip_bb"
mk_short "$work/chip_short"; mk_zero "$work/chip_zero"

PLAUSIBLE=16777216   # MIN_SIZE in the bin -- a real, plausible pnor size.

run_case() {  # $1=name $2=want-substring $3=state $4=pnor(yes/no) $5=size $6=mux $7=chip-fixture $8=read-fail(yes/no,opt) $9=cmdlog(opt) $10=ro(yes/no,opt) $11=mux-real(yes/no,opt)
    local name="$1" want="$2" state="$3" pnor="$4" size="$5" mux="$6" chip="$7"
    local readfail="${8:-no}" cmdlog="${9:-}" ro="${10:-no}" muxreal="${11:-no}"
    local out
    out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
          FIX_STATE="$state" FIX_PNOR="$pnor" FIX_MTD="mtd5" \
          FIX_SIZE="$size" FIX_MUX="$mux" FIX_CHIP="$chip" \
          FIX_READ_FAIL="$readfail" FIX_CMDLOG="$cmdlog" FIX_RO="$ro" \
          FIX_MUX_REAL="$muxreal" \
          "$work/bin" probe 1.2.3.4 2>&1)
    if [[ "$out" == *"$want"* ]]; then
        echo "ok   - $name"; pass=$((pass+1))
    else
        echo "FAIL - $name"; echo "       want: $want"; echo "       got:  $out"; fail=$((fail+1))
    fi
}

run_case "populated chip reports populated" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop"

run_case "all-0xFF chip reports blank" \
    '"chip":"blank"' Off yes "$PLAUSIBLE" 0 "$work/chip_blank"

run_case "erased top block reports bootblock_blank" \
    '"chip":"bootblock_blank"' Off yes "$PLAUSIBLE" 0 "$work/chip_bb"

run_case "powered-on host refuses" \
    '"error":"host_not_off"' On yes "$PLAUSIBLE" 0 "$work/chip_pop"

# ssh_unreachable: the power-state command itself returns nothing, as it
# would if ssh dropped before any output arrived.
cmdlog="$work/cmdlog_ssh_unreachable"; : > "$cmdlog"
run_case "unreachable BMC reports ssh_unreachable" \
    '"error":"ssh_unreachable"' "" yes "$PLAUSIBLE" 0 "$work/chip_pop" no "$cmdlog"
# Minor (2nd round review): host_not_off was the only case asserting the bus
# was never touched -- ssh_unreachable exits even earlier and deserves the
# same guard, not just an inference from the other case.
if grep -qE 'gpio|1e630000\.spi' "$cmdlog"; then
    echo "FAIL - a bus/mux command was sent on a path that never took the bus (ssh_unreachable)"
    echo "       cmdlog: $(cat "$cmdlog")"
    fail=$((fail+1))
else
    echo "ok   - no bus/mux command sent on the ssh_unreachable path"; pass=$((pass+1))
fi

run_case "no pnor after bind is bind_failed" \
    '"error":"bind_failed"' Off no "$PLAUSIBLE" 0 "$work/chip_pop"

# CORRECTION (dispatcher, prior round): the size-plausibility check runs
# UNCONDITIONALLY -- it is never gated on FLAX_PROBE_BLOCKS (which only
# overrides how many blocks are read once a plausible chip is found).
# FLAX_PROBE_BLOCKS=8 is still set here (via run_case) to prove that
# overriding the block count does NOT suppress the size check.
run_case "implausible pnor size is chip_absent regardless of FLAX_PROBE_BLOCKS" \
    '"error":"chip_absent"' Off yes 1024 0 "$work/chip_pop"

# Critical 2 regression guard: a read that comes back non-numeric (ssh drop,
# EIO, device busy mid-pipeline) must be reported as read_failed, NEVER
# coerced to a byte count of 0 -- which would read as "blank" and authorise
# writing a BIOS image onto a chip nobody actually looked at.
run_case "failed read reports read_failed, never blank" \
    '"error":"read_failed"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop" yes
# Confirm it is specifically NOT the pre-fix bug: a failed read must never
# produce a "blank" verdict.
out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
      FIX_STATE=Off FIX_PNOR=yes FIX_MTD="mtd5" FIX_SIZE="$PLAUSIBLE" \
      FIX_MUX=0 FIX_CHIP="$work/chip_pop" FIX_READ_FAIL=yes \
      "$work/bin" probe 1.2.3.4 2>&1)
if [[ "$out" == *'"chip":"blank"'* ]]; then
    echo "FAIL - a failed read must never be reported as a blank verdict"; fail=$((fail+1))
else
    echo "ok   - a failed read is not silently reported as blank"; pass=$((pass+1))
fi

# Important (2nd round review): the non-numeric check above does NOT catch a
# read that fails at RUNTIME -- dd hitting EIO/device-busy/a stale ro node
# discards stderr and simply produces an empty or truncated pipe, and wc -c
# reports that as a perfectly numeric byte count. Two such "reads" would
# still sail through the old check and read as {"chip":"blank"}. Guarded now
# by requiring the RAW byte count to be exactly 65536 before the non-0xFF
# count is trusted at all. These two fixtures are genuinely short/empty
# files -- the real dd|tr|wc pipeline runs against them for real, so this is
# not a simulated failure.
run_case "short read (32768 of 65536 bytes) reports read_failed, never blank" \
    '"error":"read_failed"' Off yes "$PLAUSIBLE" 0 "$work/chip_short"
out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
      FIX_STATE=Off FIX_PNOR=yes FIX_MTD="mtd5" FIX_SIZE="$PLAUSIBLE" \
      FIX_MUX=0 FIX_CHIP="$work/chip_short" \
      "$work/bin" probe 1.2.3.4 2>&1)
if [[ "$out" == *'"chip":"blank"'* ]]; then
    echo "FAIL - a short read must never be reported as a blank verdict"; fail=$((fail+1))
else
    echo "ok   - a short read is not silently reported as blank"; pass=$((pass+1))
fi

run_case "zero-byte read reports read_failed, never blank" \
    '"error":"read_failed"' Off yes "$PLAUSIBLE" 0 "$work/chip_zero"
out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
      FIX_STATE=Off FIX_PNOR=yes FIX_MTD="mtd5" FIX_SIZE="$PLAUSIBLE" \
      FIX_MUX=0 FIX_CHIP="$work/chip_zero" \
      "$work/bin" probe 1.2.3.4 2>&1)
if [[ "$out" == *'"chip":"blank"'* ]]; then
    echo "FAIL - a zero-byte read must never be reported as a blank verdict"; fail=$((fail+1))
else
    echo "ok   - a zero-byte read is not silently reported as blank"; pass=$((pass+1))
fi

run_case "mux stuck at BMC is mux_stuck" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" 1 "$work/chip_pop"

# Critical 1 regression guard: an unreadable mux value (the gpio directory
# already gone, or garbage) must be treated as mux_stuck, NEVER silently as
# "at PCH". This is the exact bug: the old code's readback fell back to
# "echo 0" whenever the gpio directory was absent, so a successful teardown
# always read as success regardless of the actual pin state.
run_case "unreadable mux state (gpio absent) is mux_stuck, never silently OK" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" absent "$work/chip_pop"

# Minor (2nd round review): the case above is still simulated -- the stub
# answers FIX_MUX directly rather than running restore_bus's own remote
# script. A mutant reverting that script's "else echo absent" back to
# "else echo 0" (Critical 1's exact conflation, reintroduced INSIDE the
# remote body) passed all tests, because none of them executed the real
# script. FIX_MUX_REAL=yes closes that: the stub runs the ACTUAL remote
# restore_bus command for real (same mechanism already used for the real
# dd|tr|wc read pipeline), against this dev host's own filesystem.
# /sys/class/gpio does not exist here, so the real script's own "else echo
# absent" branch fires -- this is the genuine remote body being exercised,
# not a canned answer.
run_case "real remote restore script: gpio absent here really answers absent -> mux_stuck" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop" no "" no yes

# Important 5: assert the restore command was actually SENT on the mux_stuck
# path -- not just that the bin fabricated an error locally.
#
# Important 4 regression guard (minor, 2nd round review): a single-burst
# regression -- the trap going back to being a no-op -- must also fail a
# test, not just "was it sent at all". restore_bus retries 3 times
# explicitly in step 6, and (per Important 4's fix) the EXIT trap retries
# ANOTHER 3 times when that first burst never confirmed PCH -- 6 total
# gpio/unexport calls. Counting exactly 6 catches a regression to a single
# burst (3) as readily as a regression back to no retry at all (1).
cmdlog="$work/cmdlog_mux_stuck"; : > "$cmdlog"
run_case "mux_stuck path actually sends the restore command" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" 1 "$work/chip_pop" no "$cmdlog"
n=$(grep -c 'gpio/unexport' "$cmdlog")
if [ "$n" = 6 ]; then
    echo "ok   - restore command was sent 6 times (3 explicit + 3 from the trap)"; pass=$((pass+1))
else
    echo "FAIL - expected 6 restore attempts (3 explicit + 3 trap retries), got $n"; fail=$((fail+1))
fi

# Important 3 regression guard: on paths that never took the bus
# (ssh_unreachable, host_not_off), the bin must never touch the mux or the
# spi driver at all -- the bus was never taken, so there is nothing to
# restore, and restore_bus must be a pure no-op (no remote call).
cmdlog="$work/cmdlog_not_off"; : > "$cmdlog"
run_case "powered-on host: bus is never touched (no restore command sent)" \
    '"error":"host_not_off"' On yes "$PLAUSIBLE" 0 "$work/chip_pop" no "$cmdlog"
# The power-state check itself always appears in the log -- that's legitimate
# (it runs before the bus/mux decision is even made). What must NEVER appear
# on this path is anything that mutates the gpio or the spi driver.
if grep -qE 'gpio|1e630000\.spi' "$cmdlog"; then
    echo "FAIL - a bus/mux command was sent on a path that never took the bus"
    echo "       cmdlog: $(cat "$cmdlog")"
    fail=$((fail+1))
else
    echo "ok   - no bus/mux command sent on a path that never took the bus"; pass=$((pass+1))
fi

# Regression guard for spec 2.5: a HEALTHY image has 12 MiB of all-0xFF
# interior. Sampling the middle would call a good chip blank.
mk_pop "$work/chip_hole"
dd if=/dev/zero bs=64k count=4 2>/dev/null | tr '\0' '\377' \
   | dd of="$work/chip_hole" bs=64k seek=2 conv=notrunc 2>/dev/null
run_case "blank interior is still populated" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_hole"

# ro-node preference (coordinator resolved Minor 7 against a live BMC,
# 172.17.10.101: /dev carries mtd<N>ro alongside mtd<N> for every MTD).
# Prefer the ro twin -- a free never-write guarantee at the device-node
# level -- but never require it: pnor only exists during the mux window, so
# its ro twin can't be verified ahead of time, and a hard dependency on an
# unconfirmed node would turn a safety nicety into an outage.
# NOTE: the log also carries the ro-EXISTENCE-CHECK command itself
# ("[ -e /dev/mtd5ro ] && echo yes"), which legitimately mentions "mtd5ro"
# on BOTH paths -- checking for it is not the same as USING it. The
# assertions below grep specifically for the "dd if=..." READ command, never
# just for the device name anywhere in the log.
cmdlog="$work/cmdlog_ro_present"; : > "$cmdlog"
run_case "ro node present: reads go through mtd5ro" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop" no "$cmdlog" yes
if grep -q 'dd if=/dev/mtd5ro ' "$cmdlog"; then
    echo "ok   - read pipeline used the ro node when present"; pass=$((pass+1))
else
    echo "FAIL - read pipeline did not use the ro node when present"
    echo "       cmdlog: $(cat "$cmdlog")"; fail=$((fail+1))
fi

cmdlog="$work/cmdlog_ro_absent"; : > "$cmdlog"
run_case "ro node absent: falls back to mtd5 and still verdicts correctly" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop" no "$cmdlog" no
if grep -q 'dd if=/dev/mtd5ro ' "$cmdlog"; then
    echo "FAIL - read pipeline used a ro node that was reported absent"; fail=$((fail+1))
elif grep -q 'dd if=/dev/mtd5 ' "$cmdlog"; then
    echo "ok   - read pipeline fell back to the plain node when ro is absent"; pass=$((pass+1))
else
    echo "FAIL - read pipeline referenced neither node as expected"
    echo "       cmdlog: $(cat "$cmdlog")"; fail=$((fail+1))
fi

# No failure path may emit a chip key (worker contract). Reuses the
# bind_failed scenario, which is a genuine error path.
out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
      FIX_STATE=Off FIX_PNOR=no FIX_MTD="mtd5" \
      FIX_SIZE="$PLAUSIBLE" FIX_MUX=0 FIX_CHIP="$work/chip_pop" \
      "$work/bin" probe 1.2.3.4 2>&1)
if [[ "$out" == *'"chip"'* ]]; then
    echo "FAIL - error record must not carry a chip key"; fail=$((fail+1))
else
    echo "ok   - error record carries no chip key"; pass=$((pass+1))
fi

echo; echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
