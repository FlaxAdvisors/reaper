#!/bin/bash
# Tests for bmc-clear-entity-cache, via the FLAX_BMC_REMOTE_EXEC seam: the
# remote command runs for real against a fixture standing in for
# /var/configuration/system.json, so the rm actually executes.
#
# Run: bash scripts/test-bmc-clear-entity-cache.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0

sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-clear-entity-cache.sh.j2" > "$work/bin"
chmod +x "$work/bin"

cat > "$work/stub" <<'STUB'
#!/bin/bash
[ -n "${STUB_FAIL:-}" ] && exit 255
cmd=$(printf '%s' "$2" | sed -e "s#/var/configuration/system.json#$FIX_CACHE#g")
eval "$cmd"
STUB
chmod +x "$work/stub"

check() {  # name expect_substring actual
    if printf '%s' "$3" | grep -qF "$2"; then
        printf '  PASS  %s\n' "$1"; pass=$((pass+1))
    else
        printf '  FAIL  %s\n        want: %s\n        got:  %s\n' "$1" "$2" "$3"; fail=$((fail+1))
    fi
}

echo "bmc-clear-entity-cache"

# The cache is present and must actually be removed -- not merely reported.
printf '{"stale":"config"}' > "$work/cache"
out=$(FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_CACHE="$work/cache" "$work/bin" clear 10.0.0.1 2>&1)
check "reports cleared when the cache existed" '"cleared":true' "$out"
if [ -e "$work/cache" ]; then
    printf '  FAIL  the cache file is actually gone\n        got:  file still present\n'; fail=$((fail+1))
else
    printf '  PASS  the cache file is actually gone\n'; pass=$((pass+1))
fi

# Idempotent: a unit with no cache is already in the desired state, and a
# firmware update must not fail because there was nothing to delete.
rm -f "$work/cache"
out=$(FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_CACHE="$work/cache" "$work/bin" clear 10.0.0.1 2>&1)
check "already-absent cache is success, not an error" '"cleared":true' "$out"

# An unreachable BMC must say so distinctly, so the caller can record a reason
# rather than guessing.
out=$(FLAX_BMC_REMOTE_EXEC="$work/stub" STUB_FAIL=1 FIX_CACHE="$work/cache" "$work/bin" clear 10.0.0.1 2>&1)
check "unreachable BMC reports ssh_unreachable" '"error":"ssh_unreachable"' "$out"

# ── restart subcommand: never rm, reports restarted / restart_failed / ssh_unreachable ──
ok()  { pass=$((pass+1)); printf '  PASS  %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL  %s\n' "$1"; }

cat > "$work/rstub" <<'STUB'
#!/bin/bash
[ -n "${FIX_CMDLOG:-}" ] && printf '%s\n' "$2" >> "$FIX_CMDLOG"
printf '%s\n' "${FIX_OUT:-}"
exit "${FIX_RC:-0}"
STUB
chmod +x "$work/rstub"

run_restart() {  # run_restart <name> -- runs `restart`, sets $out $rc; logs the remote command to $FIX_CMDLOG
    export FIX_CMDLOG="$work/cmd.$1"; : > "$FIX_CMDLOG"
    out=$(FLAX_BMC_REMOTE_EXEC="$work/rstub" "$work/bin" restart 10.0.0.1 2>"$work/err.$1"); rc=$?
}

FIX_OUT=RESTARTED FIX_RC=0 run_restart rok
if [ $rc -eq 0 ] && [ "$out" = '{"restarted":true}' ] && ! grep -q 'rm ' "$FIX_CMDLOG"; then ok "restart restarts EM and never deletes system.json"; else bad "restart restarts EM and never deletes system.json"; fi
FIX_OUT= FIX_RC=1 run_restart rfail
if [ $rc -eq 1 ] && [ "$out" = '{"error":"restart_failed"}' ]; then ok "a failed systemctl is restart_failed"; else bad "a failed systemctl is restart_failed"; fi
FIX_OUT= FIX_RC=255 run_restart rdown
if [ $rc -eq 1 ] && [ "$out" = '{"error":"ssh_unreachable"}' ]; then ok "ssh 255 is ssh_unreachable"; else bad "ssh 255 is ssh_unreachable"; fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
