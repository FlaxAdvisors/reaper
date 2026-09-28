#!/bin/bash
# Tests for bmc-fw-update (reaper replacement for ghost's bin). Runs the REAL bin
# against stubs for Redfish (FLAX_REDFISH_EXEC), the artifact fetch
# (FLAX_FETCH_EXEC), ping (FLAX_PING_EXEC) and flax-forget-port (FLAX_FORGET_BIN).
#
# What this suite gates:
#   a. the output records bmc_fw parses are unchanged (phase/percent/task_id/state)
#   b. success = the BMC returns at a NEW version, through the activation drop
#   c. every busy condition exits 3 with InterlockBusy and never POSTs
#   d. a 423 is never answered with a PATCH or a second POST
#   e. --port runs flax-forget-port on success; the credential never reaches argv
#
# Run: bash scripts/test-bmc-fw-update.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0
# Task 3 (2026-09-27): every invocation in this suite -- including the ones
# that do not go through run() -- keeps the manual claim, the reboot marker
# and bmc_fw's own claim dir under $work. Nothing here may touch /run/flax.
export FLAX_MANUAL_CLAIM_DIR="$work/manual" FLAX_REBOOT_DIR="$work/reboot" FLAX_CLAIM_DIR="$work/active"

sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bmc-fw-update.sh.j2" > "$work/bin"
chmod +x "$work/bin"
iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%S+00:00; }

cat > "$work/rf" <<'STUB'
#!/bin/bash
method="$1"; path="$2"; shift 2
printf '%s %s %s\n' "$method" "$path" "$*" >> "$FIX_CMDLOG"
case "$method $path" in
  "GET /redfish/v1/TaskService/Tasks")
      printf '%s\nHTTP=%s' "${FIX_TASKS_COLL:-{\"Members\":[]\}}" "${FIX_TASKS_CODE:-200}" ;;
  "GET /redfish/v1/TaskService/Tasks/1") printf '%s\nHTTP=200' "$FIX_OLD_TASK" ;;
  "GET /redfish/v1/TaskService/Tasks/9")
      n=$(cat "$FIX_SEQN" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$FIX_SEQN"
      # Task 3: at the FIRST task poll (mid-run, before the write commits),
      # record what the claim/marker dirs look like, and optionally let
      # "another run" overwrite the manual claim (FIX_CLAIM_STEAL).
      if [ "$n" = 1 ] && [ -n "${FIX_CLAIMLOG:-}" ]; then
          [ -e "$FLAX_MANUAL_CLAIM_DIR/et10b1" ] && echo "claim_seen_during_run $(cat "$FLAX_MANUAL_CLAIM_DIR/et10b1")" >> "$FIX_CLAIMLOG"
          [ -e "$FLAX_REBOOT_DIR/et10b1" ] && [ "$(stat -c %Y "$FLAX_REBOOT_DIR/et10b1")" -ge "${FIX_T0:-0}" ] && echo "marker_before_commit" >> "$FIX_CLAIMLOG"
          [ -n "${FIX_CLAIM_STEAL:-}" ] && printf '%s\n' "$FIX_CLAIM_STEAL" > "$FLAX_MANUAL_CLAIM_DIR/et10b1"
          echo "claims_during_run $(ls -A "$FLAX_MANUAL_CLAIM_DIR" 2>/dev/null | tr '\n' ' ')" >> "$FIX_CLAIMLOG"
      fi
      line=$(sed -n "${n}p" "$FIX_SEQ"); [ -n "$line" ] || line=$(tail -n 1 "$FIX_SEQ")
      set -- $line
      if [ "$1" = "DOWN" ]; then printf '\nHTTP=000'
      else printf '{"Id":"9","TaskState":"%s","PercentComplete":%s}\nHTTP=200' "$1" "$2"; fi ;;
  "POST /redfish/v1/UpdateService/update")
      printf 'HTTP/1.1 202 Accepted\r\nLocation: /redfish/v1/TaskService/Tasks/9\r\n\r\n{}\nHTTP=%s' "${FIX_POST_CODE:-202}" ;;
  "GET /redfish/v1/UpdateService/FirmwareInventory/bmc_active")
      n=$(cat "$FIX_VERN" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$FIX_VERN"
      if [ "$n" -le 1 ]; then printf '{"Version":"%s"}\nHTTP=200' "${FIX_PRE:-flax-onetree-1.1.1}"
      else printf '{"Version":"%s"}\nHTTP=200' "${FIX_POST:-flax-onetree-1.1.2}"; fi ;;
  *) printf '\nHTTP=404' ;;
esac
STUB
printf '#!/bin/bash\n[ -n "${FIX_FETCH_FAIL:-}" ] && exit 22\nprintf tar > "$2"\n' > "$work/fetch"
printf '#!/bin/bash\nexit 0\n' > "$work/ping"
printf '#!/bin/bash\necho "$1" >> "$FIX_FORGOT"\n' > "$work/forget"
chmod +x "$work/rf" "$work/fetch" "$work/ping" "$work/forget"

run() {  # run <name> [extra flash args...]
    local name="$1"; shift
    export FIX_CMDLOG="$work/cmd.$name" FIX_SEQN="$work/seqn.$name" FIX_VERN="$work/vern.$name" FIX_FORGOT="$work/forgot.$name"
    export FIX_CLAIMLOG="$work/log.$name" FIX_T0; FIX_T0=$(date +%s)
    : > "$FIX_CMDLOG"; : > "$FIX_FORGOT"; : > "$FIX_CLAIMLOG"; rm -f "$FIX_SEQN" "$FIX_VERN"
    out=$(FLAX_REDFISH_EXEC="$work/rf" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" \
          FLAX_FORGET_BIN="$work/forget" BMC_FW_POLL_SECS=0 BMC_FW_ACT_POLL_SECS=0 \
          BMC_FW_ACTIVATION_WAIT="${ACTW:-5}" BMC_FW_UPDATE_LOCK_DIR="$work" \
          "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar --port et10b1 "$@" 2>"$work/err.$name")
    rc=$?
}
run_same() { local name="$1"; shift; run "$name" --same "$@"; }
ok()  { pass=$((pass + 1)); echo "ok   $1"; }
bad() { fail=$((fail + 1)); echo "FAIL $1"; echo "     rc=$rc out=$(echo "$out" | tail -n 3) err=$(tail -n 2 "$work/err.$2" 2>/dev/null)"; }
posted()  { grep -q '^POST /redfish/v1/UpdateService/update' "$FIX_CMDLOG"; }
patched() { grep -q '^PATCH' "$FIX_CMDLOG"; }
now=$(date +%s)
export FIX_OLD_TASK='{}'

# ── a/b/e: the happy path through the activation drop ───────────────────────
printf 'New 0\nNew 40\nNew 90\nDOWN\n' > "$work/seq.ok"
FIX_SEQ="$work/seq.ok" run happy
[ $rc -eq 0 ] && echo "$out" | tail -n 1 | grep -q '{"phase":"activated","percent":100,"task_id":"9","state":"flax-onetree-1.1.2"}' \
  && ok "progress -> drop -> returns at new version -> activated, exit 0" || bad "happy path" happy
echo "$out" | grep -q '^{"phase":"monitoring","percent":40,"task_id":"9","state":"New"}$' && ok "monitoring record shape unchanged" || bad "record shape" happy
grep -qx et10b1 "$work/forgot.happy" && ok "--port -> flax-forget-port et10b1" || bad "forget-port" happy

printf 'New 0\nNew 50\nDOWN\n' > "$work/seq.same"
FIX_SEQ="$work/seq.same" FIX_POST=flax-onetree-1.1.1 ACTW=2 run same
[ $rc -eq 1 ] && echo "$out" | grep -q ActivationTimeout && ok "returns at the SAME version -> ActivationTimeout, exit 1" || bad "same version" same

printf 'New 10\nException 10\n' > "$work/seq.exc"
FIX_SEQ="$work/seq.exc" run exc
[ $rc -eq 1 ] && ok "task Exception -> exit 1" || bad "exception" exc

# ── c/d: the interlock ───────────────────────────────────────────────────────
export FIX_SEQ="$work/seq.ok"
FIX_TASKS_COLL='{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}' \
FIX_OLD_TASK='{"Id":"1","TaskState":"Running","PercentComplete":30}' run running
[ $rc -eq 3 ] && ! posted && echo "$out" | grep -q InterlockBusy && grep -q job_running "$work/err.running" && ok "running job -> InterlockBusy, no push" || bad "running" running

FIX_TASKS_COLL='{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}' \
FIX_OLD_TASK="{\"Id\":\"1\",\"TaskState\":\"Completed\",\"EndTime\":\"$(iso $((now - 20)))\"}" run quiet
[ $rc -eq 3 ] && ! posted && grep -q quiet_window "$work/err.quiet" && ok "job ended 20s ago -> quiet window, no push" || bad "quiet" quiet

FIX_TASKS_CODE=503 run unread
[ $rc -eq 3 ] && ! posted && grep -q tasks_unreadable "$work/err.unread" && ok "task list unreadable -> busy, no push" || bad "unreadable" unread

FIX_POST_CODE=423 run r423
[ $rc -eq 3 ] && ! patched && [ "$(grep -c '^POST' "$FIX_CMDLOG")" -eq 1 ] && ok "423 -> InterlockBusy, no PATCH, one POST" || bad "423" r423

exec 9>"$work/fw-update-10.0.0.2.lock"; flock -n 9
run locked
[ $rc -eq 3 ] && ! posted && grep -q local_lock "$work/err.locked" && ok "local lock held -> busy, no push" || bad "lock" locked
exec 9>&-

FIX_FETCH_FAIL=1 run nofetch
[ $rc -eq 1 ] && ! posted && ok "fetch failure -> exit 1, no push" || bad "fetch" nofetch

# ── Task 3: manual runs hold a bin-owned claim; reboot marker; --same ───────
# The claim is /run/flax/bmc-fw-manual/<port> (FLAX_MANUAL_CLAIM_DIR), holding
# "<parent pid>@<host>"; its mtime is the claim time. bmc_fw's own claim dir
# (bmc-fw-active, FLAX_CLAIM_DIR) must never be touched by a bin.
mt() { stat -c %Y "$1" 2>/dev/null; }
host_now=${HOSTNAME:-$(cat /proc/sys/kernel/hostname)}

rm -rf "$work/manual" "$work/reboot" "$work/active"
FIX_SEQ="$work/seq.ok" run claim_owned
[ $rc -eq 0 ] && ok "claim_owned run succeeds" || bad "claim_owned rc" claim_owned
[ ! -e "$work/manual/et10b1" ] && ok "owned claim removed on exit" || bad "owned claim left behind" claim_owned
grep -Eq "^claim_seen_during_run [0-9]+@${host_now}\$" "$work/log.claim_owned" && ok "claim present while running, content <pid>@<host>" || bad "claim not held during run ($(cat "$work/log.claim_owned"))" claim_owned
[ -e "$work/reboot/et10b1" ] && ok "reboot marker written on commit" || bad "no reboot marker" claim_owned
! grep -q marker_before_commit "$work/log.claim_owned" && ok "reboot marker NOT written before the write committed" || bad "marker written before commit" claim_owned
[ ! -e "$work/active/et10b1" ] && ok "a bin never creates a bmc-fw-active claim" || bad "bin wrote into bmc-fw-active" claim_owned

# a pre-existing bmc-fw-active claim (bmc_fw's own) is untouched by the run
rm -rf "$work/manual" "$work/reboot" "$work/active"; mkdir -p "$work/active"
printf 'bmc_fw\n' > "$work/active/et10b1"; touch -d '@1790000000' "$work/active/et10b1"
FIX_SEQ="$work/seq.ok" run claim_foreign
[ -e "$work/active/et10b1" ] && [ "$(cat "$work/active/et10b1")" = bmc_fw ] && [ "$(mt "$work/active/et10b1")" = 1790000000 ] \
  && ok "pre-existing bmc-fw-active claim untouched (content + mtime)" || bad "bmc_fw's claim was touched" claim_foreign

# a manual claim another run wrote mid-run (two IPs -> one port) is not ours to remove
rm -rf "$work/manual" "$work/reboot"
FIX_SEQ="$work/seq.ok" FIX_CLAIM_STEAL="4242@otherhost" run claim_stolen
[ "$(cat "$work/manual/et10b1" 2>/dev/null)" = "4242@otherhost" ] && ok "manual claim holding another run's token is not removed" || bad "removed another run's manual claim" claim_stolen

# a stale manual claim (a SIGKILLed earlier run) is taken over, then removed
rm -rf "$work/manual" "$work/reboot"; mkdir -p "$work/manual"
printf '999999@deadhost\n' > "$work/manual/et10b1"; touch -d '@1790000000' "$work/manual/et10b1"
FIX_SEQ="$work/seq.ok" run claim_stale
grep -Eq "^claim_seen_during_run [0-9]+@${host_now}\$" "$work/log.claim_stale" && [ ! -e "$work/manual/et10b1" ] \
  && ok "stale manual claim taken over (fresh token) and removed on exit" || bad "stale claim handling ($(cat "$work/log.claim_stale"))" claim_stale

# the marker's mtime is refreshed by each reboot we cause
rm -rf "$work/manual" "$work/reboot"; mkdir -p "$work/reboot"; : > "$work/reboot/et10b1"; touch -d '@1790000000' "$work/reboot/et10b1"
t_before=$(date +%s)
FIX_SEQ="$work/seq.ok" run marker_refresh
[ "$(mt "$work/reboot/et10b1")" -ge "$t_before" ] && ok "existing reboot marker's mtime refreshed at commit" || bad "marker mtime not refreshed ($(mt "$work/reboot/et10b1"))" marker_refresh

# no write committed -> no marker; the claim still goes away
rm -rf "$work/manual" "$work/reboot"
FIX_SEQ="$work/seq.exc" run marker_exc
[ ! -e "$work/reboot/et10b1" ] && [ ! -e "$work/manual/et10b1" ] && ok "task Exception -> no reboot marker, claim removed" || bad "marker/claim on Exception" marker_exc
rm -rf "$work/manual" "$work/reboot"
FIX_TASKS_CODE=503 run marker_busy
[ $rc -eq 3 ] && [ ! -e "$work/reboot/et10b1" ] && [ ! -e "$work/manual/et10b1" ] && ok "interlock busy -> no marker, claim removed" || bad "marker/claim on interlock" marker_busy

# --same skips flax-forget-port (the marker is still written: the BMC still reboots)
rm -rf "$work/manual" "$work/reboot"
FIX_SEQ="$work/seq.ok" FIX_POST=flax-onetree-1.1.1 ACTW=1 run_same same_noforget
[ -e "$work/cmd.same_noforget" ] && posted && [ ! -s "$work/forgot.same_noforget" ] && ok "--same: write committed, no forget-port" || bad "--same called forget-port" same_noforget
[ -e "$work/reboot/et10b1" ] && ok "--same: reboot marker still written" || bad "--same: no marker" same_noforget
FIX_SEQ="$work/seq.ok" run upgrade_forgets --downgrade
grep -qx et10b1 "$work/forgot.upgrade_forgets" && ok "without --same (even with --downgrade): forget-port still called" || bad "forget-port skipped without --same" upgrade_forgets

# no --port -> no claim, no marker
rm -rf "$work/manual" "$work/reboot"
: > "$work/cmd.noport"
out=$(FIX_CMDLOG="$work/cmd.noport" FIX_SEQ="$work/seq.ok" FIX_SEQN="$work/seqn.noport" FIX_VERN="$work/vern.noport" FIX_FORGOT="$work/forgot.noport" \
      FLAX_REDFISH_EXEC="$work/rf" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" FLAX_FORGET_BIN="$work/forget" \
      BMC_FW_POLL_SECS=0 BMC_FW_ACT_POLL_SECS=0 BMC_FW_ACTIVATION_WAIT=5 BMC_FW_UPDATE_LOCK_DIR="$work" \
      "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar 2>"$work/err.noport"); rc=$?
[ $rc -eq 0 ] && [ -z "$(ls -A "$work/manual" 2>/dev/null)" ] && [ -z "$(ls -A "$work/reboot" 2>/dev/null)" ] && ok "no --port -> no claim, no marker" || bad "no --port wrote a claim/marker" noport

# fix round 1, M1: a repeated --port -> claim, marker and forget all on the LAST one
rm -rf "$work/manual" "$work/reboot"
FIX_SEQ="$work/seq.ok" run m1_repeat --port et0b0       # argv: --port et10b1 --port et0b0
grep -qx 'claims_during_run et0b0 ' "$work/log.m1_repeat" && [ "$(ls -A "$work/reboot" 2>/dev/null)" = et0b0 ] && grep -qx et0b0 "$work/forgot.m1_repeat" \
  && ok "repeated --port: claim, marker and forget-port all on the last port" || bad "repeated --port split ($(cat "$work/log.m1_repeat"); reboot=$(ls -A "$work/reboot" 2>/dev/null))" m1_repeat

# fix round 1, M2: a --port followed by a flag, or a malformed value, is no port
rm -rf "$work/manual" "$work/reboot"
FIX_SEQ="$work/seq.ok" FIX_POST=flax-onetree-1.1.1 ACTW=1 run m2_flag --port --same   # last --port has no value
[ ! -s "$work/forgot.m2_flag" ] && [ -z "$(ls -A "$work/manual" "$work/reboot" 2>/dev/null)" ] \
  && ok "--port --same: --same not swallowed, no forget-port, no claim/marker" || bad "--port --same swallowed ($(cat "$work/forgot.m2_flag"))" m2_flag
rm -rf "$work/manual" "$work/reboot"
FIX_SEQ="$work/seq.ok" run m2_bad --port ../x
[ ! -s "$work/forgot.m2_bad" ] && [ -z "$(ls -A "$work/manual" "$work/reboot" 2>/dev/null)" ] && [ ! -e "$work/x" ] \
  && ok "--port ../x: dropped -- no forget-port, no claim/marker file" || bad "--port ../x used ($(cat "$work/forgot.m2_bad"))" m2_bad

# fix round 1, M3: a normal exit leaves no heartbeat `sleep` behind
FIX_SEQ="$work/seq.ok" FLAX_CLAIM_HEARTBEAT_S=37.514 run m3_sleep
sleep 0.3
[ $rc -eq 0 ] && ! pgrep -f 'sleep 37\.514' >/dev/null && ok "normal exit: no orphaned heartbeat sleep" \
  || { bad "heartbeat sleep survived a normal exit: $(pgrep -af 'sleep 37\.514')" m3_sleep; pkill -f 'sleep 37\.514'; }

# a trailing --port with no value must not wedge the arg loop
rm -rf "$work/manual" "$work/reboot"
FIX_SEQ="$work/seq.ok" FIX_CMDLOG="$work/cmd.dangle" FIX_SEQN="$work/seqn.dangle" FIX_VERN="$work/vern.dangle" FIX_FORGOT="$work/forgot.dangle" \
  FLAX_REDFISH_EXEC="$work/rf" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" FLAX_FORGET_BIN="$work/forget" \
  BMC_FW_POLL_SECS=0 BMC_FW_ACT_POLL_SECS=0 BMC_FW_ACTIVATION_WAIT=5 BMC_FW_UPDATE_LOCK_DIR="$work" \
  timeout 20 "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar --port >/dev/null 2>&1; rc=$?
[ $rc -ne 124 ] && [ -z "$(ls -A "$work/manual" 2>/dev/null)" ] && ok "trailing --port with no value: terminates (rc=$rc), no claim" || bad "trailing --port wedged or claimed" dangle

# ── version subcommand ───────────────────────────────────────────────────────
v=$(FLAX_REDFISH_EXEC="$work/rf" FIX_CMDLOG="$work/cmd.v" FIX_VERN="$work/vern.v" "$work/bin" version 10.0.0.2)
[ "$v" = "flax-onetree-1.1.1" ] && ok "version -> bmc_active Version" || { rc=x; out=$v; bad "version" v; }

# ── the credential never reaches argv ────────────────────────────────────────
grep -l 'test-dummy' "$work"/cmd.* >/dev/null 2>&1 && { rc=x; bad "credential in a call's args" x; } || ok "credential absent from every recorded call"
refs=$(grep -n 'SSHPASS' "$work/bin" | grep -vE '^\s*[0-9]+:\s*#' | grep -vE 'export SSHPASS=|login \$RF_USER password \$SSHPASS"')
[ -z "$refs" ] && ok "SSHPASS used only via export and the netrc heredoc" || { rc=x; out="$refs"; bad "SSHPASS use" x; }


# ── the lock directory: sudo re-exec, never a fallback dir (2026-09-24) ─────
rodir="$work/ro-lockdir"; mkdir -p "$rodir"; chmod 555 "$rodir"
printf '#!/bin/bash\nexit 1\n' > "$work/nosudo"
printf '#!/bin/bash\n[ "$1" = "-n" ] && [ "$2" = "true" ] && exit 0\nprintf "%%s\\n" "$*" > "$FIX_SUDOLOG"\nexit 0\n' > "$work/fakesudo"
chmod +x "$work/nosudo" "$work/fakesudo"
if [ "$(id -u)" != 0 ]; then
    : > "$work/cmd.nosudo"
    o=$(env FIX_CMDLOG="$work/cmd.nosudo" FLAX_SUDO="$work/nosudo" BMC_FW_UPDATE_LOCK_DIR="$rodir" FLAX_REDFISH_EXEC="$work/rf" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" \
        "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar 2>&1); r=$?
    if [ $r -eq 2 ] && [[ "$o" == *"cannot write the lock directory"* ]] && ! grep -qE '^(POST|RF POST)|i2cset' "$work/cmd.nosudo"; then
        echo "ok   - unwritable lock dir, no sudo -> exit 2, nothing sent"; pass=$((pass+1))
    else echo "FAIL - unwritable lock dir, no sudo (rc=$r out=$o)"; fail=$((fail+1)); fi
    export FIX_SUDOLOG="$work/sudolog"; : > "$FIX_SUDOLOG"
    env FLAX_SUDO="$work/fakesudo" BMC_FW_UPDATE_LOCK_DIR="$rodir" FLAX_REDFISH_EXEC="$work/rf" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar >/dev/null 2>&1; r=$?
    if [ $r -eq 0 ] && grep -q -- "-n $work/bin flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar" "$FIX_SUDOLOG"; then
        echo "ok   - unwritable lock dir -> re-runs itself under sudo -n with the same args"; pass=$((pass+1))
    else echo "FAIL - sudo re-exec (rc=$r log=$(cat "$FIX_SUDOLOG"))"; fail=$((fail+1)); fi
else
    echo "ok   - (skipped sudo re-exec cases: running as root)"; pass=$((pass+1))
fi
chmod 755 "$rodir"

# ── fix round 3, M4: this bin got the SAME parent/child restructuring
#     (round 2) as bios-fw-update / bmc-blade-power-cycle, but carried NO
#     dedicated test of its own -- dropping --pdeathsig, dropping the
#     spawn's 9>&-, or reverting the wait loop all left this suite at
#     16/0. These port the bios-fw-update cases: SIGKILL of the TOP-LEVEL
#     pid only (mid-flash, hung in busy_check's interlock read), kill the
#     CHILD directly (TERM and KILL), and a closed-stdout case. ──────────
cat > "$work/rf.hang" <<'EOF'
#!/bin/bash
method="$1"; path="$2"; shift 2
[ -n "${FIX_CMDLOG:-}" ] && printf '%s %s %s\n' "$method" "$path" "$*" >> "$FIX_CMDLOG"
case "$method $path" in
  "GET /redfish/v1/TaskService/Tasks") exec sleep "$HANG_DUR" ;;
  *) printf '\nHTTP=404' ;;
esac
EOF
chmod +x "$work/rf.hang"
lockfile_bmcfw="$work/fw-update-10.0.0.2.lock"

# --- SIGKILL of the TOP-LEVEL pid only, hung in busy_check's interlock
#     read: the child must be gone fast, the lock free immediately, and
#     no surviving process may hold fd 9. ---
rm -f "$lockfile_bmcfw"
cmdlog_bk="$work/cmd.bmcfw-sigkill"; : > "$cmdlog_bk"
( HANG_DUR=81.418 FLAX_REDFISH_EXEC="$work/rf.hang" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" \
  FLAX_FORGET_BIN="$work/forget" BMC_FW_UPDATE_LOCK_DIR="$work" FIX_CMDLOG="$cmdlog_bk" \
  "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar >/dev/null 2>"$work/err.bmcfw-sigkill" ) &
bp=$!
for i in $(seq 1 100); do pgrep -f 'sleep 81\.418' >/dev/null 2>&1 && break; sleep 0.1; done
pgrep -f 'sleep 81\.418' >/dev/null 2>&1 || { echo "FAIL - bmc-fw-update SIGKILL setup: the hang never started"; fail=$((fail+1)); }
childp_bk=""
for i in $(seq 1 50); do childp_bk=$(pgrep -P "$bp" 2>/dev/null | head -1); [ -n "$childp_bk" ] && break; sleep 0.1; done
t0=$(date +%s)
kill -9 "$bp"
child_gone_bk=0
for i in $(seq 1 20); do
    [ -n "$childp_bk" ] && { kill -0 "$childp_bk" 2>/dev/null || { child_gone_bk=1; break; }; }
    [ -z "$childp_bk" ] && { child_gone_bk=1; break; }
    sleep 0.1
done
dt=$(( $(date +%s) - t0 ))
if [ "$child_gone_bk" = 1 ] && [ "$dt" -le 3 ]; then
    ok "SIGKILL of the top-level pid: the child is gone within ${dt}s (--pdeathsig KILL)"
else
    rc=x; bad "SIGKILL of the top-level pid: child $childp_bk still alive after ${dt}s" bmcfw-sigkill
fi
if flock -n "$lockfile_bmcfw" true; then
    ok "lock free immediately after SIGKILL of the top-level pid"
else
    rc=x; bad "lock still held after SIGKILL of the top-level pid" bmcfw-sigkill
fi
holder_bk=$(for p in /proc/[0-9]*; do pid=${p#/proc/}; [ -e "$p/fd/9" ] || continue; tgt=$(readlink "$p/fd/9" 2>/dev/null); [ "$tgt" = "$lockfile_bmcfw" ] && echo "$pid"; done)
if [ -z "$holder_bk" ]; then
    ok "no surviving process holds fd 9 on the lock file after SIGKILL"
else
    rc=x; bad "fd 9 still held by:$holder_bk" bmcfw-sigkill
fi
if grep -q '^POST' "$cmdlog_bk" 2>/dev/null; then
    rc=x; bad "a POST was sent despite the SIGKILL" bmcfw-sigkill
else
    ok "no POST sent after SIGKILL mid-busy_check"
fi
pkill -9 -f 'sleep 81\.418' 2>/dev/null

# --- N1: TERM (then KILL) sent to the CHILD directly, bypassing the
#     parent. This bin's CHILD branch installs no TERM/INT/HUP trap of
#     its own, so a direct TERM hits default disposition and terminates
#     immediately even mid-hang (same reasoning as bmc-blade-power-cycle;
#     unlike bios-fw-update, which needs a short hang because its CHILD
#     traps TERM to clean $ART). ---
kill_child_test_bmcfw() {  # kill_child_test_bmcfw <signal-name> <sleep-duration>
    local sig="$1" dur="$2" fp; fp=$(echo "$dur" | sed 's/\./\\./g')
    ( HANG_DUR="$dur" FLAX_REDFISH_EXEC="$work/rf.hang" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" \
      FLAX_FORGET_BIN="$work/forget" BMC_FW_UPDATE_LOCK_DIR="$work" \
      "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar >/dev/null 2>&1 ) &
    local bp=$! i childp=""
    for i in $(seq 1 100); do pgrep -f "sleep $fp" >/dev/null 2>&1 && break; sleep 0.1; done
    for i in $(seq 1 50); do childp=$(pgrep -P "$bp" 2>/dev/null | head -1); [ -n "$childp" ] && break; sleep 0.1; done
    if [ -z "$childp" ]; then rc=x; bad "kill-child-bmcfw-$sig: could not find the child pid" x; kill -9 "$bp" 2>/dev/null; return; fi
    kill -s "$sig" "$childp"
    local t0 dt still_alive=1
    t0=$(date +%s)
    for i in $(seq 1 40); do
        kill -0 "$bp" 2>/dev/null || { still_alive=0; break; }
        sleep 0.1
    done
    dt=$(( $(date +%s) - t0 ))
    if [ "$still_alive" = 1 ]; then
        rc=x; bad "kill-child-bmcfw-$sig: parent still alive after ${dt}s (N1 spin?)" x
        kill -9 "$bp" "$childp" 2>/dev/null
    else
        wait "$bp" 2>/dev/null; local wrc=$?
        if [ "$wrc" -ne 0 ]; then
            ok "kill-child-bmcfw-$sig: parent exits within ${dt}s, non-zero status ($wrc)"
        else
            rc=x; bad "kill-child-bmcfw-$sig: parent exited with status 0 (unexpected)" x
        fi
    fi
    pkill -9 -f "sleep $fp" 2>/dev/null
}
kill_child_test_bmcfw TERM 82.529
kill_child_test_bmcfw KILL 83.631

# --- N1: a downstream reader closing stdout early must not spin the
#     parent (the CHILD dies of SIGPIPE). Process substitution keeps $!
#     tracking the bin's own pid. A normal, fast happy-path run. ---
printf 'New 0\nNew 40\nNew 90\nDOWN\n' > "$work/seq.closedout"
: > "$work/cmd.closedout"
FIX_SEQ="$work/seq.closedout" FLAX_REDFISH_EXEC="$work/rf" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" \
  FLAX_FORGET_BIN="$work/forget" BMC_FW_POLL_SECS=0 BMC_FW_ACT_POLL_SECS=0 BMC_FW_ACTIVATION_WAIT=5 \
  BMC_FW_UPDATE_LOCK_DIR="$work" FIX_CMDLOG="$work/cmd.closedout" FIX_SEQN="$work/seqn.closedout" FIX_VERN="$work/vern.closedout" FIX_FORGOT="$work/forgot.closedout" \
  "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar --port et10b1 > >(head -c1 >/dev/null) 2>/dev/null &
bpid=$!
t0=$(date +%s)
still_alive=1
for i in $(seq 1 40); do
    kill -0 "$bpid" 2>/dev/null || { still_alive=0; break; }
    sleep 0.1
done
dt=$(( $(date +%s) - t0 ))
if [ "$still_alive" = 1 ]; then
    rc=x; bad "closed stdout: parent still alive after ${dt}s (N1 spin?)" x
    kill -9 "$bpid" 2>/dev/null
else
    ok "closed stdout: parent exits promptly (${dt}s), no spin"
fi
wait "$bpid" 2>/dev/null

# ── Task 3: the manual claim lives and dies with the PARENT (lock holder) ────
# TERM to the parent -> claim removed. SIGKILL to the parent -> nothing can
# remove it; it stays with the parent's token and its mtime = the claim time,
# so reconcile's 2700 s bound expires it. The token is the PARENT's pid (the
# pid callers see), which is what proves the parent -- not the child --
# created it.
claim_signal_test_bmcfw() {  # <TERM|KILL> <sleep-duration>
    local sig="$1" dur="$2" fp; fp=$(echo "$dur" | sed 's/\./\\./g')
    rm -rf "$work/manual" "$work/reboot"
    local t_start; t_start=$(date +%s)
    ( HANG_DUR="$dur" FLAX_REDFISH_EXEC="$work/rf.hang" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" \
      FLAX_FORGET_BIN="$work/forget" BMC_FW_UPDATE_LOCK_DIR="$work" FLAX_CLAIM_HEARTBEAT_S=1 \
      "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar --port et10b1 >/dev/null 2>"$work/err.claim$sig" ) &
    local bp=$! i
    for i in $(seq 1 100); do pgrep -f "sleep $fp" >/dev/null 2>&1 && break; sleep 0.1; done
    local tok_during; tok_during=$(cat "$work/manual/et10b1" 2>/dev/null)
    [ "$tok_during" = "$bp@$host_now" ] && ok "claim-$sig: claim held during the run with the PARENT's token" \
        || { rc=x; bad "claim-$sig: claim during run = '$tok_during', want '$bp@$host_now'" "claim$sig"; }
    # heartbeat: FLAX_CLAIM_HEARTBEAT_S=1, so over 2.5 s the mtime must move
    local m1 m2; m1=$(mt "$work/manual/et10b1"); sleep 2.5; m2=$(mt "$work/manual/et10b1")
    [ -n "$m1" ] && [ -n "$m2" ] && [ "$m2" -gt "$m1" ] && ok "claim-$sig: heartbeat advances the claim's mtime while the parent lives ($m1 -> $m2)" \
        || { rc=x; bad "claim-$sig: heartbeat did not advance mtime ($m1 -> $m2)" "claim$sig"; }
    local ticker; ticker=$(for c in $(pgrep -P "$bp"); do grep -q USR1 "/proc/$c/cmdline" 2>/dev/null && echo "$c"; done)
    [ -n "$ticker" ] && ok "claim-$sig: heartbeat ticker running under the parent" || { rc=x; bad "claim-$sig: no ticker found" "claim$sig"; }
    local t_kill; t_kill=$(date +%s)
    kill -s "$sig" "$bp"
    for i in $(seq 1 40); do kill -0 "$bp" 2>/dev/null || break; sleep 0.1; done
    wait "$bp" 2>/dev/null; local wrc=$?
    if [ "$sig" = TERM ]; then
        [ "$wrc" -eq 143 ] && [ ! -e "$work/manual/et10b1" ] && ok "claim-TERM: parent exits 143 and removes its claim" \
            || { rc=x; bad "claim-TERM: rc=$wrc claim=$(cat "$work/manual/et10b1" 2>/dev/null)" claimTERM; }
    else
        local m3 m4; m3=$(mt "$work/manual/et10b1"); sleep 2.5; m4=$(mt "$work/manual/et10b1")
        [ "$(cat "$work/manual/et10b1" 2>/dev/null)" = "$bp@$host_now" ] && [ -n "$m3" ] && [ "$m3" -ge "$t_start" ] && [ "$m3" -le "$t_kill" ] \
            && ok "claim-KILL: claim left behind with the parent's token, mtime = last heartbeat before the kill" \
            || { rc=x; bad "claim-KILL: claim=$(cat "$work/manual/et10b1" 2>/dev/null) mtime=$m3 start=$t_start kill=$t_kill" claimKILL; }
        [ "$m4" = "$m3" ] && ok "claim-KILL: mtime stops advancing once the parent is dead (goes stale)" \
            || { rc=x; bad "claim-KILL: mtime still advancing after SIGKILL ($m3 -> $m4)" claimKILL; }
    fi
    local left=""; for c in $ticker; do kill -0 "$c" 2>/dev/null && left="$left $c"; done
    [ -z "$left" ] && ok "claim-$sig: heartbeat ticker gone with the parent" || { rc=x; bad "claim-$sig: ticker survived:$left" "claim$sig"; kill -9 $left 2>/dev/null; }
    [ ! -e "$work/reboot/et10b1" ] && ok "claim-$sig: no reboot marker (nothing was written)" || { rc=x; bad "claim-$sig: marker written" "claim$sig"; }
    pkill -9 -f "sleep $fp" 2>/dev/null
}
claim_signal_test_bmcfw TERM 84.137
claim_signal_test_bmcfw KILL 85.241

echo "---"; echo "pass=$pass fail=$fail"
[ $fail -eq 0 ]
