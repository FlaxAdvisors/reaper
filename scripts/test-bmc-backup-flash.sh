#!/bin/bash
# Tests for bmc-backup-flash's version reader.
#
# The bin's real work happens in a pipeline executed ON the BMC
# (dd | strings | grep). These tests run that EXACT pipeline against fixture
# files standing in for /dev/mtd0, /dev/mtd5, /etc/os-release and /proc/mtd,
# via the FLAX_BMC_REMOTE_EXEC seam -- so what is under test is the real
# command string the bin builds, not a re-implementation of it.
#
# Run: bash scripts/test-bmc-backup-flash.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0

# Render the Jinja template with a dummy credential -- never a real one.
sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-backup-flash.sh.j2" > "$work/bin"
chmod +x "$work/bin"

# The stub: rewrite device/file paths to fixtures, then run the command for
# real. This is what keeps the test honest -- grep, strings and dd all execute.
cat > "$work/stub" <<'STUB'
#!/bin/bash
# sed with a '#' delimiter: the paths are full of slashes, and bash's
# ${var//a/b} cannot express a pattern containing the delimiter cleanly.
cmd=$(printf '%s' "$2" | sed \
    -e "s#/dev/mtd0#$FIX_MTD0#g" \
    -e "s#/dev/mtd5#$FIX_MTD5#g" \
    -e "s#/etc/os-release#$FIX_OSREL#g" \
    -e "s#/proc/mtd#$FIX_PROCMTD#g")
eval "$cmd"
STUB
chmod +x "$work/stub"

mk_chip() { printf 'JUNKJUNKJUNK\n        "machine": "tiogapass",\n        "version": "%s"\nMOREJUNK\n' "$1" > "$2"; }
mk_blank() { head -c 4096 /dev/zero | tr '\0' 'Z' > "$1"; }

run_case() {  # name active_os mtd0_ver mtd5_ver expect_substring
    local name="$1" osver="$2" m0="$3" m5="$4" want="$5"
    printf 'VERSION_ID=%s\n' "$osver" > "$work/osrel"
    if [ "$m0" = "BLANK" ]; then mk_blank "$work/m0"; else mk_chip "$m0" "$work/m0"; fi
    if [ "$m5" = "BLANK" ]; then mk_blank "$work/m5"; else mk_chip "$m5" "$work/m5"; fi
    printf 'dev:    size   erasesize  name\nmtd0: 04000000 00010000 "bmc"\nmtd5: 04000000 00010000 "bmc-backup"\n' > "$work/procmtd"
    local out
    out=$(FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_MTD0="$work/m0" FIX_MTD5="$work/m5" \
          FIX_OSREL="$work/osrel" FIX_PROCMTD="$work/procmtd" \
          "$work/bin" version 10.0.0.1 2>&1)
    if printf '%s' "$out" | grep -qF "$want"; then
        printf '  PASS  %s\n' "$name"; pass=$((pass+1))
    else
        printf '  FAIL  %s\n        want substring: %s\n        got: %s\n' "$name" "$want" "$out"; fail=$((fail+1))
    fi
}

echo "bmc-backup-flash version reader"

# THE BUG: builds ship as plain semver with no -<datestamp>. The reader must
# not require a build stamp to trust its own canary.
run_case "plain-semver build reads its backup" \
    "flax-onetree-1.1.1" "flax-onetree-1.1.1" "flax-onetree-1.1.1-202608281924" \
    '"backup_version":"flax-onetree-1.1.1-202608281924"'

# Regression guard: the stamped form must keep working.
run_case "stamped build still reads its backup" \
    "flax-onetree-1.1.1-202608281924" "flax-onetree-1.1.1-202608281924" "flax-onetree-1.1.1" \
    '"backup_version":"flax-onetree-1.1.1"'

# The canary must still fire when the reader really is broken: mtd0 does not
# contain what /etc/os-release says it should, so the chip-select mapping is
# untrusted and NOTHING may be written.
run_case "canary fires when mtd0 disagrees with os-release" \
    "flax-onetree-1.1.1" "flax-onetree-9.9.9" "flax-onetree-1.1.1" \
    '"error":"reader_canary_failed"'

# A blank backup chip must report read_failed, NOT canary failure: that is the
# narrow signal bmcfw.is_chip_blank uses to authorise the initial write.
run_case "blank backup chip reports read_failed" \
    "flax-onetree-1.1.1" "flax-onetree-1.1.1" "BLANK" \
    '"error":"read_failed"'

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
