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

# The stub. FIX_STATE/FIX_PNOR/FIX_MTD/FIX_SIZE/FIX_MUX/FIX_CHIP describe one
# scenario; the stub answers each command shape the bin sends, rewriting the
# pnor device path to a fixture file for the actual dd|tr|wc read pipeline so
# that pipeline runs for real, not re-implemented here.
cat > "$work/stub" <<'STUB'
#!/bin/bash
cmd="$2"
case "$cmd" in
  *CurrentPowerState*)
      echo "$FIX_STATE" ;;
  *'= pnor'*)
      [ "$FIX_PNOR" = yes ] && echo "$FIX_MTD" ;;
  *'/size'*)
      echo "$FIX_SIZE" ;;
  *'else echo 0'*)
      # mux gpio readback -- the only command that both reads and reports a
      # value in this shape ("cat .../value; else echo 0").
      echo "$FIX_MUX" ;;
  *'dd if=/dev/'*)
      echo "$cmd" | sed "s#/dev/$FIX_MTD#$FIX_CHIP#" | bash ;;
  *)
      # bind/unbind/mux-set bookkeeping: output is always discarded by the
      # bin, so let it run (harmlessly failing against a nonexistent
      # /sys/class/gpio on this dev host) rather than special-case it.
      echo "$cmd" | bash ;;
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

mk_pop "$work/chip_pop"; mk_blank "$work/chip_blank"; mk_bootblank "$work/chip_bb"

PLAUSIBLE=16777216   # MIN_SIZE in the bin -- a real, plausible pnor size.

run_case() {  # $1=name $2=want-substring $3=state $4=pnor(yes/no) $5=size $6=mux $7=chip-fixture
    local name="$1" want="$2" state="$3" pnor="$4" size="$5" mux="$6" chip="$7"
    local out
    out=$(FLAX_PROBE_BLOCKS=8 FLAX_BMC_REMOTE_EXEC="$work/stub" \
          FIX_STATE="$state" FIX_PNOR="$pnor" FIX_MTD="mtd5" \
          FIX_SIZE="$size" FIX_MUX="$mux" FIX_CHIP="$chip" \
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

run_case "no pnor after bind is bind_failed" \
    '"error":"bind_failed"' Off no "$PLAUSIBLE" 0 "$work/chip_pop"

# CORRECTION: the size-plausibility check runs UNCONDITIONALLY -- it is never
# gated on FLAX_PROBE_BLOCKS (which only overrides how many blocks are read
# once a plausible chip is found). FLAX_PROBE_BLOCKS=8 is still set here (via
# run_case) to prove that overriding the block count does NOT suppress the
# size check.
run_case "implausible pnor size is chip_absent regardless of FLAX_PROBE_BLOCKS" \
    '"error":"chip_absent"' Off yes 1024 0 "$work/chip_pop"

run_case "mux stuck at BMC is mux_stuck" \
    '"error":"mux_stuck"' Off yes "$PLAUSIBLE" 1 "$work/chip_pop"

# Regression guard for spec 2.5: a HEALTHY image has 12 MiB of all-0xFF
# interior. Sampling the middle would call a good chip blank.
mk_pop "$work/chip_hole"
dd if=/dev/zero bs=64k count=4 2>/dev/null | tr '\0' '\377' \
   | dd of="$work/chip_hole" bs=64k seek=2 conv=notrunc 2>/dev/null
run_case "blank interior is still populated" \
    '"chip":"populated"' Off yes "$PLAUSIBLE" 0 "$work/chip_hole"

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
