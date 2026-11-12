#!/bin/bash
# Tests for bmc-mac-factoryreset's Task 5 interlocks (Part 3): the per-BMC fw
# lock and the Redfish job busy check must both run BEFORE any BMC contact and
# refuse with a readable reason on exit 4. Runs the REAL bin against a
# FLAX_REDFISH_EXEC stub; no real ssh/Redfish call is ever made in these
# cases, so this suite never touches a BMC.
#
# Moved from ghost (post/ + playbooks/roles/utils/files/bin/) 2026-11-06; the bin
# is now the vault-templated bmc-mac-factoryreset.sh.j2 beside this test,
# rendered here with a dummy credential like test-bmc-fw-update.sh.
#
# Run: bash scripts/test-bmc-mac-factoryreset.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
bin="$work/bmc-mac-factoryreset"
sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-mac-factoryreset.sh.j2" > "$bin"
chmod +x "$bin"
pass=0; fail=0

chk() {   # name condition...
    local n="$1"; shift
    if "$@"; then printf '  PASS  %s\n' "$n"; pass=$((pass+1))
    else printf '  FAIL  %s\n        rc=%s out=%s\n' "$n" "$(cat "$work/rc" 2>/dev/null)" "$(cat "$work/err" 2>/dev/null)"; fail=$((fail+1)); fi
}

# Redfish stub: echoes back what net_busy_check needs. RF_TASK, if set, is the
# body for Tasks/1 and makes the collection report one member; otherwise the
# task collection is empty (idle).
cat > "$work/rf-idle" <<'STUB'
#!/bin/bash
case "$2" in
  */TaskService/Tasks) echo '{"Members":[]}' ;;
  *)                    echo '{}' ;;
esac
STUB
cat > "$work/rf-busy" <<'STUB'
#!/bin/bash
case "$2" in
  */TaskService/Tasks) echo '{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}' ;;
  */Tasks/1)            echo "${RF_TASK}" ;;
  *)                    echo '{}' ;;
esac
STUB
chmod +x "$work/rf-idle" "$work/rf-busy"

run() {   # run <bmc> [args...]  -- always with a stub redfish exec and a scratch lock dir
    # Proof-of-no-ssh (controller instruction: tests must exit before any ssh
    # in the refusal cases): time the run. The real bsh() has ConnectTimeout=8,
    # so a run that returns in well under that never reached the ssh call.
    local t0 t1
    t0=$(date +%s%N)
    "$bin" "$@" > "$work/out" 2> "$work/err"; echo $? > "$work/rc"
    t1=$(date +%s%N); echo $(( (t1 - t0) / 1000000 )) > "$work/ms"
}

# ── (a) lock held ───────────────────────────────────────────────────────────
mkdir -p "$work/lock"
(
  exec 9>"$work/lock/fw-update-172.17.8.101.lock"
  flock -n 9
  BMC_FW_UPDATE_LOCK_DIR="$work/lock" FLAX_REDFISH_EXEC="$work/rf-idle" run 172.17.8.101 --dry-run
)
chk "lock held: rc 4"                    sh -c '[ "$(cat "'"$work"'/rc")" = 4 ]'
chk "lock held: readable refusal"        grep -q "refusing: another firmware update holds the lock for this BMC" "$work/err"
chk "lock held: no ssh attempted (fast)" sh -c '[ "$(cat "'"$work"'/ms")" -lt 3000 ]'

# ── (b) Redfish job running ──────────────────────────────────────────────────
rm -rf "$work/lock2"; mkdir -p "$work/lock2"
RF_TASK='{"Id":"7","TaskState":"Running","PercentComplete":40}' \
  BMC_FW_UPDATE_LOCK_DIR="$work/lock2" FLAX_REDFISH_EXEC="$work/rf-busy" run 172.17.8.101 --dry-run
chk "busy job: rc 4"                     sh -c '[ "$(cat "'"$work"'/rc")" = 4 ]'
chk "busy job: readable refusal"         grep -q "refusing: BMC FW update job 7 running (Running 40%) on 172.17.8.101" "$work/err"
chk "busy job: no ssh attempted (fast)"  sh -c '[ "$(cat "'"$work"'/ms")" -lt 3000 ]'

# Same refusal without --dry-run: both interlocks run unconditionally.
rm -rf "$work/lock3"; mkdir -p "$work/lock3"
RF_TASK='{"Id":"9","TaskState":"Starting","PercentComplete":0}' \
  BMC_FW_UPDATE_LOCK_DIR="$work/lock3" FLAX_REDFISH_EXEC="$work/rf-busy" run 172.17.8.101
chk "busy job (no --dry-run): rc 4"      sh -c '[ "$(cat "'"$work"'/rc")" = 4 ]'
chk "busy job (no --dry-run): refusal"   grep -q "refusing: BMC FW update job 9 running (Starting 0%) on 172.17.8.101" "$work/err"

# quiet_window / tasks_unreadable share the generic "refusing: <reason> on <bmc>" form
cat > "$work/rf-unreadable" <<'STUB'
#!/bin/bash
echo 'not json'
STUB
chmod +x "$work/rf-unreadable"
rm -rf "$work/lock4"; mkdir -p "$work/lock4"
BMC_FW_UPDATE_LOCK_DIR="$work/lock4" FLAX_REDFISH_EXEC="$work/rf-unreadable" run 172.17.8.101 --dry-run
chk "tasks unreadable: rc 4"             sh -c '[ "$(cat "'"$work"'/rc")" = 4 ]'
chk "tasks unreadable: readable refusal" grep -q "refusing: tasks_unreadable on 172.17.8.101" "$work/err"

# ── fix round 1: lock keyed on the resolved IPv4, not the literal argv ──────
# A PATH-first fake `getent` (the bin has no resolver seam -- it shells out to
# the real tool, so tests fake it the same way).
mkdir -p "$work/fakebin"
cat > "$work/fakebin/getent" <<'STUB'
#!/bin/bash
if [ "$1" = ahostsv4 ] && [ "$2" = "bmc-host.example" ]; then
  echo "172.17.8.101 STREAM bmc-host.example"
  exit 0
fi
exit 2   # real getent: not found
STUB
chmod +x "$work/fakebin/getent"

# hostname resolving to the same IP as an IPv4 invocation must take the SAME
# lock file -- prove it by pre-holding the IP-keyed lock and invoking by name.
rm -rf "$work/lock5"; mkdir -p "$work/lock5"
(
  exec 9>"$work/lock5/fw-update-172.17.8.101.lock"
  flock -n 9
  PATH="$work/fakebin:$PATH" BMC_FW_UPDATE_LOCK_DIR="$work/lock5" FLAX_REDFISH_EXEC="$work/rf-idle" \
    run bmc-host.example --dry-run
)
chk "hostname -> same lock key as its IPv4: rc 4"       sh -c '[ "$(cat "'"$work"'/rc")" = 4 ]'
chk "hostname -> same lock key as its IPv4: refused"    grep -q "refusing: another firmware update holds the lock for this BMC" "$work/err"

# an unresolvable host/address refuses cleanly, before any lock or BMC contact
rm -rf "$work/lock6"; mkdir -p "$work/lock6"
PATH="$work/fakebin:$PATH" BMC_FW_UPDATE_LOCK_DIR="$work/lock6" FLAX_REDFISH_EXEC="$work/rf-idle" \
  run nosuchbmc.example --dry-run
chk "unresolvable host: rc 4"              sh -c '[ "$(cat "'"$work"'/rc")" = 4 ]'
chk "unresolvable host: readable refusal"  grep -q "refusing: cannot map nosuchbmc.example to the BMC's IPv4 for the firmware lock; pass --lock-ip <its IPv4> (the address the fw tools use)" "$work/err"
chk "unresolvable host: no lock file created" sh -c '[ -z "$(ls -A "'"$work"'/lock6" 2>/dev/null)" ]'

# a lock dir that cannot be opened/created is a distinct failure (exit 1) from
# a lock that is held (exit 4) -- Part 1's need_lock_dir/take_lock split.
rm -rf "$work/lock7"; mkdir -p "$work/lock7/ro"; chmod 555 "$work/lock7/ro"
BMC_FW_UPDATE_LOCK_DIR="$work/lock7/ro/nested" FLAX_REDFISH_EXEC="$work/rf-idle" run 172.17.8.101 --dry-run
chk "unwritable lock dir: rc 1 (not 4)"    sh -c '[ "$(cat "'"$work"'/rc")" = 1 ]'
chk "unwritable lock dir: readable error"  grep -q "error: cannot open lock" "$work/err"
chmod 755 "$work/lock7/ro"

# ── fix round 2: --lock-ip escape hatch for an LL-only (pinned-MAC) BMC ─────
LL='fe80::9a03:9bff:fea6:fef8%eth1.17'

# LL input + --lock-ip <ipv4>: keys the lock on the GIVEN ip (getent never
# runs -- there is nothing in $work/fakebin to resolve an LL address anyway,
# so this also proves getent is skipped, not just that it would fail).
rm -rf "$work/lock8"; mkdir -p "$work/lock8"
(
  exec 9>"$work/lock8/fw-update-172.17.8.101.lock"
  flock -n 9
  FLAX_REDFISH_EXEC="$work/rf-idle" BMC_FW_UPDATE_LOCK_DIR="$work/lock8" \
    run "$LL" --lock-ip 172.17.8.101 --dry-run
)
chk "LL + --lock-ip: uses the given ip's lock file: rc 4" sh -c '[ "$(cat "'"$work"'/rc")" = 4 ]'
chk "LL + --lock-ip: refused (lock held)"                 grep -q "refusing: another firmware update holds the lock for this BMC" "$work/err"

# LL input, no --lock-ip: unmappable -> the --lock-ip-pointing message, rc 4,
# before any lock file or BMC contact.
rm -rf "$work/lock9"; mkdir -p "$work/lock9"
FLAX_REDFISH_EXEC="$work/rf-idle" BMC_FW_UPDATE_LOCK_DIR="$work/lock9" run "$LL" --dry-run
chk "LL, no --lock-ip: rc 4"             sh -c '[ "$(cat "'"$work"'/rc")" = 4 ]'
chk "LL, no --lock-ip: readable refusal" grep -q "refusing: cannot map $LL to the BMC's IPv4 for the firmware lock; pass --lock-ip <its IPv4> (the address the fw tools use)" "$work/err"
chk "LL, no --lock-ip: no lock file created" sh -c '[ -z "$(ls -A "'"$work"'/lock9" 2>/dev/null)" ]'

# bad --lock-ip (not a dotted IPv4): usage error, rc 2, before any lock/BMC contact.
FLAX_REDFISH_EXEC="$work/rf-idle" run 172.17.8.101 --lock-ip not-an-ip
chk "bad --lock-ip: rc 2"            sh -c '[ "$(cat "'"$work"'/rc")" = 2 ]'
chk "bad --lock-ip: usage message"   grep -q "usage: bmc-mac-factoryreset" "$work/err"
chk "bad --lock-ip: names the bad value" grep -q "not-an-ip" "$work/err"

echo "-- $pass passed, $fail failed --"
[ "$fail" -eq 0 ]
