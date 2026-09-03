#!/bin/bash
# Tests for bmc-bios-chip-probe. Runs the REAL remote read/decide script (one
# dd per window into a temp file, an all-0xFF reference block generated and
# md5-identified in the same round trip, and `cmp -s` as the decision) against
# fixture chips; a single stub, driven by env vars, stands in for the BMC side
# of every command the bin sends -- power state, mux/bind bookkeeping, MTD
# discovery, size, the ro-node check, and the two 64 KiB read windows. Same
# shape as test-bmc-backup-flash.sh's FLAX_BMC_REMOTE_EXEC stub: one stub
# script, rewired per case with env vars, not a fresh heredoc per case.
#
# THE POINT OF THIS SUITE. A `blank` verdict causes another agent to write a
# BIOS image, so a false `blank` reflashes a working machine. Five earlier
# designs each closed one failure stage and left the adjacent one open,
# because every one of them decided the verdict by COUNTING bytes -- and every
# count is also a legitimate value, so "measured zero" and "did not measure"
# were indistinguishable. The bin now decides with `cmp -s`, whose rc 2 (and
# 127, and "") is an error value that is not a verdict. This suite therefore
# breaks EVERY stage that can fail -- the dd, the temp file, the reference
# block's generation, its md5 identification, cmp itself, cmp's binary, and
# the transport -- ONE STAGE AT A TIME, FOR ONE WINDOW AT A TIME, and requires
# read_failed with no "chip" key at all.
#
# PER-WINDOW IS NOT OPTIONAL (review finding). The previous suite's gates were
# mutually masking: deleting only the head gate passed 31/0 and deleting only
# the top gate passed 31/0, because the only fixtures exercising them were
# broken for BOTH windows at once. Every stage below is broken for head alone
# and for top alone, keyed on `skip=` -- the same thing the bin's own two
# read_window calls are distinguished by.
#
# Run: bash scripts/test-bmc-bios-chip-probe.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
# A directory that is NEVER created. Used to sandbox the one place a test
# runs a REAL remote script body (FIX_MUX_REAL below): every /sys path in
# that script is rewritten under this root before execution, so the test
# never touches whatever /sys/class/gpio genuinely contains on the host
# running the suite -- it does not rely on this dev host merely happening
# not to export the pin (review finding, minor).
export FIX_SYSROOT="$work/fakesys"
pass=0; fail=0

# Render the Jinja template with a dummy credential -- never a real one.
# NOTE: '|' is not special inside a '/'-delimited sed BRE, so the pattern
# below (which itself contains '|') works unescaped -- unlike a '|'-delimited
# sed, which would collide with it.
sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-bios-chip-probe.sh.j2" > "$work/bin"
chmod +x "$work/bin"

# The stub. FIX_STATE/FIX_PNOR/FIX_MTD/FIX_SIZE/FIX_MUX/FIX_CHIP/FIX_RO
# describe one scenario; the stub answers each command shape the bin sends.
# FIX_CMDLOG, when set, gets every command the bin sends appended to it, so a
# case can assert a particular remote call was (or was not) actually made.
#
# FIX_MUX_REAL=yes is one exception to "answer, don't execute": it makes the
# gpio/unexport case run the REAL remote restore_bus body, so that command's
# own internal branching gets exercised, not just simulated via FIX_MUX
# (review finding: a mutant that reverted the remote script's "else echo
# absent" back to "else echo 0" was undetected by any test, because every
# test answered FIX_MUX directly). Every /sys path in the script is rewritten
# under $FIX_SYSROOT (a directory that is never created) before it runs, so
# this is sandboxed -- not dependent on this dev host happening not to export
# the pin -- and the real "absent" branch fires deterministically because that
# rewritten path can never exist.
#
# FIX_BREAK_HEAD / FIX_BREAK_TOP each name ONE stage of the REAL remote
# read_window script to break, for ONE window only:
#
#   dd        truncate the materialized read to 32768 bytes (a real short
#             read: the real dd/trap/wc/cmp chain still runs). NOTE it uses
#             `dd bs=32k count=1`, NOT `head -c` -- BusyBox head has no -c,
#             confirmed on the live BMC, and the test must not model the
#             remote script with a flag the remote platform lacks.
#   tmp       point the temp file at a directory that does not exist, so the
#             read is never materialized at all (read-only /tmp, ENOSPC).
#   ref       break the reference block's generation -> an EMPTY reference.
#   refzero   degrade the reference generator to a passthrough -> a reference
#             of 65536 0x00 bytes. This is the ONE way `cmp` could hand back a
#             false "blank": paired with chip_zeros below (what the SPI bus
#             reads with the mux at PCH), a degraded reference makes an
#             all-0x00 window compare IDENTICAL. Caught by the md5 identity
#             check, not by any count.
#   md5       break the reference's identification (md5sum missing/killed).
#   cmp       make cmp itself ERROR (rc 2) -- its own, structurally distinct
#             failure signal, which no byte count could ever provide.
#   cmpgone   make the cmp binary itself missing (rc 127).
#   transport the far end answers garbage / drops -- no clean triple at all.
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
      # remote call (the readback must happen before the gpio directory is
      # removed, in the same round trip). FIX_MUX is the value the pin reads
      # back as; "absent" simulates a readback that cannot be trusted -- the
      # bin must treat that as mux_stuck, never silently as PCH.
      if [ "${FIX_MUX_REAL:-no}" = yes ]; then
          # Sandboxed (see FIX_SYSROOT above): rewrite the real script's
          # absolute /sys paths under a directory that is never created,
          # before executing it for real.
          sandboxed=$(echo "$cmd" | sed \
              -e "s#/sys/class/gpio#${FIX_SYSROOT:-/nonexistent}/class/gpio#g" \
              -e "s#/sys/bus/platform/drivers/spi-aspeed-smc#${FIX_SYSROOT:-/nonexistent}/bus/spi-aspeed-smc#g")
          # Log the POST-rewrite command distinctly from the pre-rewrite
          # $cmd already logged above, so a test can assert the rewrite
          # itself actually happened, not just that SOME command ran.
          # $sandboxed is multi-line, so it is fenced between markers rather
          # than prefixed on one line (its own first line is blank).
          if [ -n "${FIX_CMDLOG:-}" ]; then
              { echo "===SANDBOXED-BEGIN==="; printf '%s\n' "$sandboxed"; echo "===SANDBOXED-END==="; } >> "$FIX_CMDLOG"
          fi
          echo "$sandboxed" | bash
      else
          echo "$FIX_MUX"
      fi ;;
  *'dd if=/dev/'*)
      # read_window. The REAL remote script is run here against a fixture
      # file, with at most ONE stage deliberately broken for THIS window.
      # "skip=" in the command is exactly how the bin's own read_window()
      # calls distinguish the top window from the head window.
      case "$cmd" in
          *'skip='*) brk="${FIX_BREAK_TOP:-}" ;;
          *)         brk="${FIX_BREAK_HEAD:-}" ;;
      esac
      if [ "$brk" = transport ]; then
          echo "dd: read error: Input/output error"
      else
          # Match /dev/mtd<N> with an OPTIONAL "ro" suffix as ONE token, not
          # just the "/dev/mtd5" prefix -- a plain substring match would also
          # hit inside "/dev/mtd5ro" and leave a stray "ro" tacked onto the
          # fixture path. NOTE: "(ro)?" -- NOT "ro?", which only makes the
          # trailing "o" optional and requires a literal "r", so it silently
          # fails to match a plain (non-ro) path at all.
          rewritten=$(echo "$cmd" | sed -E "s#/dev/mtd[0-9]+(ro)?#$FIX_CHIP#")
          case "$brk" in
              dd)      rewritten=$(echo "$rewritten" | sed 's#> "$f"#| dd bs=32k count=1 2>/dev/null > "$f"#') ;;
              tmp)     rewritten=$(echo "$rewritten" | sed 's#^f=/tmp/#f=/nonexistent-dir/#') ;;
              ref)     rewritten=$(echo "$rewritten" | sed "s#tr '.000' '.377'#false#") ;;
              refzero) rewritten=$(echo "$rewritten" | sed "s#tr '.000' '.377'#cat#") ;;
              md5)     rewritten=$(echo "$rewritten" | sed 's#md5sum#false#') ;;
              cmp)     rewritten=$(echo "$rewritten" | sed 's#^cmp -s .*#cmp -s /nonexistent-a /nonexistent-b#') ;;
              cmpgone) rewritten=$(echo "$rewritten" | sed 's#^cmp -s #no-such-cmp-binary-here #') ;;
              '')      : ;;
              *)       echo "STUB-ERROR: unknown break stage: $brk" >&2; exit 9 ;;
          esac
          echo "$rewritten" | bash
      fi ;;
  *)
      # take_bus's bind sequence: fire-and-forget, output discarded by the
      # bin. Never executed for real -- this stub must never touch this test
      # host's own /sys/class/gpio, and the bin doesn't need it to for a
      # correct verdict.
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
mk_zero()  { : > "$1"; }                                                            # 0 bytes: dd reads nothing at all
# All-0x00, NOT all-0xFF: what the SPI bus reads when the mux is still at the
# PCH and JEDEC never answered. A correct reference block makes this
# "populated"; a DEGRADED reference block (refzero) makes it "blank".
mk_zeros() { dd if=/dev/zero bs=64k count=8 2>/dev/null > "$1"; }

mk_pop "$work/chip_pop"; mk_blank "$work/chip_blank"; mk_bootblank "$work/chip_bb"
mk_short "$work/chip_short"; mk_zero "$work/chip_zero"; mk_zeros "$work/chip_zeros"

PLAUSIBLE=16777216   # MIN_SIZE in the bin -- a real, plausible pnor size.

run_case() {  # $1=name $2=want-substring $3=state $4=pnor(yes/no) $5=size $6=mux $7=chip-fixture $8=cmdlog(opt) $9=ro(yes/no,opt) $10=mux-real(yes/no,opt)
    local name="$1" want="$2" state="$3" pnor="$4" size="$5" mux="$6" chip="$7"
    local cmdlog="${8:-}" ro="${9:-no}" muxreal="${10:-no}"
    local out
    out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
          FIX_STATE="$state" FIX_PNOR="$pnor" FIX_MTD="mtd5" \
          FIX_SIZE="$size" FIX_MUX="$mux" FIX_CHIP="$chip" \
          FIX_BREAK_HEAD="" FIX_BREAK_TOP="" \
          FIX_CMDLOG="$cmdlog" FIX_RO="$ro" FIX_MUX_REAL="$muxreal" \
          "$work/bin" probe 1.2.3.4 2>&1)
    if [[ "$out" == *"$want"* ]]; then
        echo "ok   - $name"; pass=$((pass+1))
    else
        echo "FAIL - $name"; echo "       want: $want"; echo "       got:  $out"; fail=$((fail+1))
    fi
}

# The mutation harness: break ONE stage, in ONE window, and require
# read_failed AND no "chip" key at all. Both halves are part of the SAME
# assertion on purpose -- the hazard is not "a wrong error code", it is a
# VERDICT (of any flavour) produced from a stage that did not run. Naming a
# stage the stub does not know is a hard STUB-ERROR, so a typo here can never
# silently become a green "unbroken" run.
#   $1 = test name  $2 = head|top  $3 = stage  $4 = chip fixture
break_case() {
    local name="$1" win="$2" stage="$3" chip="$4" out bh="" bt=""
    if [ "$win" = head ]; then bh="$stage"; else bt="$stage"; fi
    out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
          FIX_STATE=Off FIX_PNOR=yes FIX_MTD="mtd5" FIX_SIZE="$PLAUSIBLE" \
          FIX_MUX=0 FIX_CHIP="$chip" \
          FIX_BREAK_HEAD="$bh" FIX_BREAK_TOP="$bt" \
          FIX_CMDLOG="" FIX_RO=no FIX_MUX_REAL=no \
          "$work/bin" probe 1.2.3.4 2>&1)
    if [[ "$out" == *'"error":"read_failed"'* && "$out" != *'"chip"'* ]]; then
        echo "ok   - $name"; pass=$((pass+1))
    else
        echo "FAIL - $name"
        echo '       want: {"error":"read_failed"} and no "chip" key'
        echo "       got:  $out"; fail=$((fail+1))
    fi
}

# ---------------------------------------------------------------- verdicts --

run_case "populated chip reports populated" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop"

run_case "all-0xFF chip reports blank" \
    '"chip":"blank"' Off yes "$PLAUSIBLE" 0 "$work/chip_blank"

run_case "erased top block reports bootblock_blank" \
    '"chip":"bootblock_blank"' Off yes "$PLAUSIBLE" 0 "$work/chip_bb"

# Regression guard for spec 2.5: a HEALTHY image has 12 MiB of all-0xFF
# interior. Sampling the middle would call a good chip blank.
mk_pop "$work/chip_hole"
dd if=/dev/zero bs=64k count=4 2>/dev/null | tr '\0' '\377' \
   | dd of="$work/chip_hole" bs=64k seek=2 conv=notrunc 2>/dev/null
run_case "blank interior is still populated" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_hole"

# An all-0x00 chip (mux still at PCH / JEDEC silent) is NOT blank. With a
# correct 0xFF reference, cmp says "differ" for both windows.
run_case "all-0x00 chip is populated, never blank" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_zeros"

# ------------------------------------------------------- pre-read refusals --

run_case "powered-on host refuses" \
    '"error":"host_not_off"' On yes "$PLAUSIBLE" 0 "$work/chip_pop"

# ssh_unreachable: the power-state command itself returns nothing, as it
# would if ssh dropped before any output arrived.
cmdlog="$work/cmdlog_ssh_unreachable"; : > "$cmdlog"
run_case "unreachable BMC reports ssh_unreachable" \
    '"error":"ssh_unreachable"' "" yes "$PLAUSIBLE" 0 "$work/chip_pop" "$cmdlog"
# ssh_unreachable exits even earlier than host_not_off and deserves the same
# "bus never touched" guard, not just an inference from the other case.
if grep -qE 'gpio|1e630000\.spi' "$cmdlog"; then
    echo "FAIL - a bus/mux command was sent on a path that never took the bus (ssh_unreachable)"
    echo "       cmdlog: $(cat "$cmdlog")"
    fail=$((fail+1))
else
    echo "ok   - no bus/mux command sent on the ssh_unreachable path"; pass=$((pass+1))
fi

run_case "no pnor after bind is bind_failed" \
    '"error":"bind_failed"' Off no "$PLAUSIBLE" 0 "$work/chip_pop"

# The size-plausibility check runs UNCONDITIONALLY -- it is never gated on
# FLAX_PROBE_BLOCKS (which only overrides how many blocks are read once a
# plausible chip is found). FLAX_PROBE_BLOCKS=8 is still set here (via
# run_case) to prove that overriding the block count does NOT suppress it.
run_case "implausible pnor size is chip_absent regardless of FLAX_PROBE_BLOCKS" \
    '"error":"chip_absent"' Off yes 1024 0 "$work/chip_pop"

# ------------------------------------------- whole-fixture read failures ----
# These read a genuinely short / genuinely empty fixture, so the real dd runs
# and comes back short for real. They are NOT per-window (a file short enough
# to truncate the head window is by construction far too short for the top
# window either), which is exactly why they are not sufficient on their own --
# see the per-stage, per-window matrix below.
run_case "short fixture (32768 of 65536 bytes) reports read_failed" \
    '"error":"read_failed"' Off yes "$PLAUSIBLE" 0 "$work/chip_short"
run_case "zero-byte fixture reports read_failed" \
    '"error":"read_failed"' Off yes "$PLAUSIBLE" 0 "$work/chip_zero"

# ------------------------------------ per-stage, per-window failure matrix --
# EVERY stage of the real remote read/decide script, broken ALONE, for ONE
# window at a time. A stage that cannot be broken in a test is a stage that
# has not been proven.

# 1. The READ itself (dd) comes back short. Caught by the raw == 65536 gate --
#    the one count that survives, and it gates a READ, never a verdict.
break_case "dd short on the HEAD window only reports read_failed" head dd "$work/chip_pop"
break_case "dd short on the TOP window only reports read_failed"  top  dd "$work/chip_pop"

# 2. The TEMP FILE cannot be created (read-only /tmp, ENOSPC): the read is
#    never materialized, so there is nothing to measure or compare.
break_case "temp file unusable on the HEAD window only reports read_failed" head tmp "$work/chip_pop"
break_case "temp file unusable on the TOP window only reports read_failed"  top  tmp "$work/chip_pop"

# 3. The REFERENCE BLOCK's generation fails outright -> an empty reference.
break_case "reference block generation broken on the HEAD window only reports read_failed" head ref "$work/chip_pop"
break_case "reference block generation broken on the TOP window only reports read_failed"  top  ref "$work/chip_pop"

# 4. The REFERENCE BLOCK degrades to 65536 0x00 bytes (tr silently reduced to
#    a passthrough). THIS IS THE ONE ROUTE BY WHICH cmp COULD RETURN A FALSE
#    "BLANK": paired with an all-0x00 chip -- what the bus reads with the mux
#    at PCH -- a zeroed reference compares IDENTICAL. Nothing about cmp's exit
#    code can see this; only the md5 IDENTITY check on the reference can.
break_case "degraded (all-zero) reference on the HEAD window only reports read_failed" head refzero "$work/chip_zeros"
break_case "degraded (all-zero) reference on the TOP window only reports read_failed"  top  refzero "$work/chip_zeros"
# ...and the both-windows form, which is the literal false-blank demonstration:
# without the md5 gate this prints {"chip":"blank"} for a chip full of 0x00.
out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
      FIX_STATE=Off FIX_PNOR=yes FIX_MTD="mtd5" FIX_SIZE="$PLAUSIBLE" \
      FIX_MUX=0 FIX_CHIP="$work/chip_zeros" \
      FIX_BREAK_HEAD=refzero FIX_BREAK_TOP=refzero \
      "$work/bin" probe 1.2.3.4 2>&1)
if [[ "$out" == *'"error":"read_failed"'* && "$out" != *'"chip"'* ]]; then
    echo "ok   - a degraded reference block can never produce a blank verdict"; pass=$((pass+1))
else
    echo "FAIL - a degraded reference block produced a verdict"
    echo "       got:  $out"; fail=$((fail+1))
fi

# 5. The reference block's IDENTIFICATION fails (md5sum missing or killed).
#    Fail CLOSED: no identified reference, no verdict.
break_case "md5sum unavailable on the HEAD window only reports read_failed" head md5 "$work/chip_pop"
break_case "md5sum unavailable on the TOP window only reports read_failed"  top  md5 "$work/chip_pop"

# 6. cmp ITSELF ERRORS (rc 2). This is the whole reason cmp replaced counting:
#    a counting pipeline that dies prints a plausible number, but cmp's error
#    is its own exit code and cannot be mistaken for either verdict.
break_case "cmp errors (rc 2) on the HEAD window only reports read_failed" head cmp "$work/chip_pop"
break_case "cmp errors (rc 2) on the TOP window only reports read_failed"  top  cmp "$work/chip_pop"

# 7. The cmp BINARY is missing entirely (rc 127) -- neither 0 nor 1, so it is
#    not a verdict either.
break_case "cmp binary missing (rc 127) on the HEAD window only reports read_failed" head cmpgone "$work/chip_pop"
break_case "cmp binary missing (rc 127) on the TOP window only reports read_failed"  top  cmpgone "$work/chip_pop"

# 8. The TRANSPORT drops / answers garbage: no clean "<raw> <sum> <rc>" triple.
break_case "transport failure on the HEAD window only reports read_failed" head transport "$work/chip_pop"
break_case "transport failure on the TOP window only reports read_failed"  top  transport "$work/chip_pop"

# ------------------------------------------------------------- the mux ------

run_case "mux stuck at BMC is mux_stuck" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" 1 "$work/chip_pop"

# An unreadable mux value (the gpio directory already gone, or garbage) must be
# treated as mux_stuck, NEVER silently as "at PCH". This is the exact bug the
# old code had: its readback fell back to "echo 0" whenever the gpio directory
# was absent, so a successful teardown always read as success regardless of the
# actual pin state.
run_case "unreadable mux state (gpio absent) is mux_stuck, never silently OK" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" absent "$work/chip_pop"

# The case above is still simulated -- the stub answers FIX_MUX directly rather
# than running restore_bus's own remote script. A mutant reverting that
# script's "else echo absent" back to "else echo 0" passed all tests, because
# none of them executed the real script. FIX_MUX_REAL=yes closes that: the stub
# runs the ACTUAL remote restore_bus command for real, with every /sys path
# rewritten under $FIX_SYSROOT (a directory the suite never creates) first.
# That rewritten path can never exist, so the real script's own "else echo
# absent" branch fires for a STRUCTURAL reason, not because this particular dev
# host happens not to export the pin -- this stays correct on a host that does.
#
# The rewrite itself was previously unasserted -- deleting the FIX_SYSROOT sed
# reddened no test on this host, so the sandbox could rot silently. cmdlog
# below captures the POST-rewrite command the stub actually ran (not the
# pre-rewrite $cmd the top-of-stub logger captures, which legitimately always
# contains the real /sys path) and asserts it targets $FIX_SYSROOT, never the
# bare /sys path.
cmdlog="$work/cmdlog_mux_real"; : > "$cmdlog"
run_case "real remote restore script: gpio absent here really answers absent -> mux_stuck" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop" "$cmdlog" no yes
sandboxed_block=$(sed -n '/===SANDBOXED-BEGIN===/,/===SANDBOXED-END===/p' "$cmdlog")
rewrite_ok=yes
[[ "$sandboxed_block" == *"$FIX_SYSROOT/class/gpio"* ]] || rewrite_ok=no
[[ "$sandboxed_block" == *"$FIX_SYSROOT/bus/spi-aspeed-smc"* ]] || rewrite_ok=no
[[ "$sandboxed_block" == *"/sys/class/gpio"* ]] && rewrite_ok=no
[[ "$sandboxed_block" == *"/sys/bus/platform/drivers/spi-aspeed-smc"* ]] && rewrite_ok=no
if [ "$rewrite_ok" = yes ]; then
    echo "ok   - the /sys rewrite actually happened before the real script ran"; pass=$((pass+1))
else
    echo "FAIL - the /sys rewrite did not happen as expected (or the raw path leaked through)"
    echo "       cmdlog block: $sandboxed_block"; fail=$((fail+1))
fi

# Assert the restore command was actually SENT on the mux_stuck path -- not
# just that the bin fabricated an error locally. A single-burst regression --
# the trap going back to being a no-op -- must also fail a test, not just "was
# it sent at all": restore_bus retries 3 times explicitly in step 6, and the
# EXIT trap retries ANOTHER 3 when that first burst never confirmed PCH -- 6
# total gpio/unexport calls. Counting exactly 6 catches a regression to a
# single burst (3) as readily as a regression back to no retry at all (1).
cmdlog="$work/cmdlog_mux_stuck"; : > "$cmdlog"
run_case "mux_stuck path actually sends the restore command" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" 1 "$work/chip_pop" "$cmdlog"
n=$(grep -c 'gpio/unexport' "$cmdlog")
if [ "$n" = 6 ]; then
    echo "ok   - restore command was sent 6 times (3 explicit + 3 from the trap)"; pass=$((pass+1))
else
    echo "FAIL - expected 6 restore attempts (3 explicit + 3 trap retries), got $n"; fail=$((fail+1))
fi

# On paths that never took the bus (ssh_unreachable, host_not_off), the bin
# must never touch the mux or the spi driver at all -- the bus was never taken,
# so there is nothing to restore, and restore_bus must be a pure no-op.
cmdlog="$work/cmdlog_not_off"; : > "$cmdlog"
run_case "powered-on host: bus is never touched (no restore command sent)" \
    '"error":"host_not_off"' On yes "$PLAUSIBLE" 0 "$work/chip_pop" "$cmdlog"
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

# ------------------------------------------------------- the ro twin node ---
# /dev carries mtd<N>ro alongside mtd<N> for every MTD (confirmed live on
# 172.17.10.101). Prefer the ro twin -- a free never-write guarantee at the
# device-node level -- but never require it: pnor only exists during the mux
# window, so its ro twin can't be verified ahead of time, and a hard dependency
# on an unconfirmed node would turn a safety nicety into an outage.
# NOTE: the log also carries the ro-EXISTENCE-CHECK command itself
# ("[ -e /dev/mtd5ro ] && echo yes"), which legitimately mentions "mtd5ro" on
# BOTH paths -- checking for it is not the same as USING it. The assertions
# below grep specifically for the "dd if=..." READ command.
cmdlog="$work/cmdlog_ro_present"; : > "$cmdlog"
run_case "ro node present: reads go through mtd5ro" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop" "$cmdlog" yes
if grep -q 'dd if=/dev/mtd5ro ' "$cmdlog"; then
    echo "ok   - read pipeline used the ro node when present"; pass=$((pass+1))
else
    echo "FAIL - read pipeline did not use the ro node when present"
    echo "       cmdlog: $(cat "$cmdlog")"; fail=$((fail+1))
fi

cmdlog="$work/cmdlog_ro_absent"; : > "$cmdlog"
run_case "ro node absent: falls back to mtd5 and still verdicts correctly" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop" "$cmdlog" no
if grep -q 'dd if=/dev/mtd5ro ' "$cmdlog"; then
    echo "FAIL - read pipeline used a ro node that was reported absent"; fail=$((fail+1))
elif grep -q 'dd if=/dev/mtd5 ' "$cmdlog"; then
    echo "ok   - read pipeline fell back to the plain node when ro is absent"; pass=$((pass+1))
else
    echo "FAIL - read pipeline referenced neither node as expected"
    echo "       cmdlog: $(cat "$cmdlog")"; fail=$((fail+1))
fi

# ------------------------------------------------------ structural checks ---

# ONE dd per window, not two. Two independent reads of the same region (one
# size-checked, one feeding the verdict) was a demonstrated Critical: the
# checked read proved nothing about the read that produced the answer.
cmdlog="$work/cmdlog_onedd"; : > "$cmdlog"
run_case "populated chip still populated (dd-count instrumentation)" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_pop" "$cmdlog"
n=$(grep -c 'dd if=/dev/mtd5 ' "$cmdlog")
if [ "$n" = 2 ]; then
    echo "ok   - exactly one device read per window (2 for 2 windows)"; pass=$((pass+1))
else
    echo "FAIL - expected exactly 2 device reads (one per window), got $n"; fail=$((fail+1))
fi

# The remote temp-file trap must cover EXIT INT TERM HUP -- on BusyBox ash
# EXIT alone does not fire for an ssh drop (SIGHUP), and /tmp is tmpfs on a
# 489 MB board.
if grep -qE "^trap 'rm -f .*' EXIT INT TERM HUP\$" "$here/bmc-bios-chip-probe.sh.j2"; then
    echo "ok   - remote temp-file trap covers EXIT INT TERM HUP"; pass=$((pass+1))
else
    echo "FAIL - remote temp-file trap is not 'EXIT INT TERM HUP'"; fail=$((fail+1))
fi

# The verdict must never be derived from a byte COUNT of the window's content.
# Five rounds of false blanks came from exactly that; the only wc left is the
# raw completeness gate on the dd output.
code_only=$(grep -v '^[[:space:]]*#' "$here/bmc-bios-chip-probe.sh.j2")
if [ "$(printf '%s\n' "$code_only" | grep -c 'wc -c')" = 1 ]; then
    echo "ok   - exactly one byte count remains (the dd completeness gate)"; pass=$((pass+1))
else
    echo "FAIL - unexpected byte-counting in the bin:"
    printf '%s\n' "$code_only" | grep -n 'wc -c'; fail=$((fail+1))
fi

# No failure path may emit a chip key (worker contract: bios_chip.state_from
# takes the FIRST record carrying one). Reuses the bind_failed scenario.
out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
      FIX_STATE=Off FIX_PNOR=no FIX_MTD="mtd5" \
      FIX_SIZE="$PLAUSIBLE" FIX_MUX=0 FIX_CHIP="$work/chip_pop" \
      FIX_BREAK_HEAD="" FIX_BREAK_TOP="" \
      "$work/bin" probe 1.2.3.4 2>&1)
if [[ "$out" == *'"chip"'* ]]; then
    echo "FAIL - error record must not carry a chip key"; fail=$((fail+1))
else
    echo "ok   - error record carries no chip key"; pass=$((pass+1))
fi

# RULE ZERO: the rendered credential line must carry NO surrounding quotes --
# ansible's `quote` filter emits its own.
if grep -q '^export SSHPASS={{ bmc_root_password | quote }}$' "$here/bmc-bios-chip-probe.sh.j2"; then
    echo "ok   - credential line is unquoted (ansible's quote filter supplies its own)"; pass=$((pass+1))
else
    echo "FAIL - credential line is not the expected unquoted form"; fail=$((fail+1))
fi

echo; echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
