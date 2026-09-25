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

run() {
    export FIX_CMDLOG="$work/cmd.$1" FIX_SEQN="$work/seqn.$1" FIX_VERN="$work/vern.$1" FIX_FORGOT="$work/forgot.$1"
    : > "$FIX_CMDLOG"; : > "$FIX_FORGOT"; rm -f "$FIX_SEQN" "$FIX_VERN"
    out=$(FLAX_REDFISH_EXEC="$work/rf" FLAX_FETCH_EXEC="$work/fetch" FLAX_PING_EXEC="$work/ping" \
          FLAX_FORGET_BIN="$work/forget" BMC_FW_POLL_SECS=0 BMC_FW_ACT_POLL_SECS=0 \
          BMC_FW_ACTIVATION_WAIT="${ACTW:-5}" BMC_FW_UPDATE_LOCK_DIR="$work" \
          "$work/bin" flash 10.0.0.2 http://share/flax-onetree-1.1.2.tar --port et10b1 2>"$work/err.$1")
    rc=$?
}
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

echo "---"; echo "pass=$pass fail=$fail"
[ $fail -eq 0 ]
