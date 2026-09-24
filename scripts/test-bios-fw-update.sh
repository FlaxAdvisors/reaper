#!/bin/bash
# Tests for bios-fw-update. Runs the REAL bin against stubs for Redfish
# (FLAX_REDFISH_EXEC), the BMC root shell (FLAX_BMC_REMOTE_EXEC) and the artifact
# fetch (FLAX_FETCH_EXEC). FIX_CMDLOG records every Redfish call, which is how the
# interlock cases prove that NO push (and no busy-flag PATCH) was ever sent.
#
# What this suite gates:
#   a. every busy condition (running job, quiet window, unreadable, 423, local
#      lock) exits 3 and never POSTs
#   b. a 423 is never answered with a PATCH of HttpPushUriTargetsBusy
#   c. the verdict is read from the journal and classified per ending
#   d. the ME-changed cut check tells a missing cut from a real one
#   e. the credential never reaches argv
#
# Run: bash scripts/test-bios-fw-update.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
pass=0; fail=0

sed 's/{{ bmc_root_password | quote }}/'"'"'test-dummy'"'"'/' \
    "$here/bios-fw-update.sh.j2" > "$work/bin"
chmod +x "$work/bin"

iso() { date -u -d "@$1" +%Y-%m-%dT%H:%M:%S+00:00; }

cat > "$work/rf" <<'STUB'
#!/bin/bash
method="$1"; path="$2"; shift 2
printf '%s %s %s\n' "$method" "$path" "$*" >> "$FIX_CMDLOG"
case "$method $path" in
  "GET /redfish/v1/TaskService/Tasks")
      printf '%s\nHTTP=%s' "${FIX_TASKS_COLL:-{\"Members\":[]\}}" "${FIX_TASKS_CODE:-200}" ;;
  "GET /redfish/v1/TaskService/Tasks/1")
      printf '%s\nHTTP=200' "$FIX_OLD_TASK" ;;
  "GET /redfish/v1/TaskService/Tasks/7")
      n=$(cat "$FIX_SEQN" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$FIX_SEQN"
      line=$(sed -n "${n}p" "$FIX_SEQ"); [ -n "$line" ] || line=$(tail -n 1 "$FIX_SEQ")
      set -- $line
      if [ "$1" = "DOWN" ]; then printf '\nHTTP=000'
      else printf '{"Id":"7","TaskState":"%s","PercentComplete":%s}\nHTTP=200' "$1" "$2"; fi ;;
  "POST /redfish/v1/UpdateService/update")
      printf '{"@odata.id":"/redfish/v1/TaskService/Tasks/7"}\nHTTP=%s' "${FIX_POST_CODE:-202}" ;;
  "GET /redfish/v1/UpdateService/FirmwareInventory/bios_active")
      printf '{"Version":"TPC_P26F"}\nHTTP=200' ;;
  *) printf '\nHTTP=404' ;;
esac
STUB
cat > "$work/ssh" <<'STUB'
#!/bin/bash
case "$2" in
  *boot_id*)
      n=$(cat "$FIX_BOOTN" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$FIX_BOOTN"
      if [ "$n" -le "${FIX_BOOT_SAME_FOR:-999}" ]; then echo "${FIX_BOOT0:-aaaa}"
      elif [ "${FIX_BOOT_AFTER:-}" = "DOWN" ]; then exit 255
      else echo "${FIX_BOOT_AFTER:-bbbb}"; fi ;;
  *journalctl*) cat "$FIX_JOURNAL" ;;
esac
STUB
cat > "$work/fetch" <<'STUB'
#!/bin/bash
[ -n "${FIX_FETCH_FAIL:-}" ] && exit 22
printf 'tarbytes' > "$2"
STUB
chmod +x "$work/rf" "$work/ssh" "$work/fetch"

run() {  # run <name> -- sets $out $rc; fixtures come from the environment
    export FIX_CMDLOG="$work/cmd.$1" FIX_SEQN="$work/seqn.$1" FIX_BOOTN="$work/bootn.$1"
    : > "$FIX_CMDLOG"; rm -f "$FIX_SEQN" "$FIX_BOOTN"
    out=$(FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/ssh" FLAX_FETCH_EXEC="$work/fetch" \
          BIOS_FW_UPDATE_POLL_S=0 BIOS_FW_UPDATE_CUT_POLL_S=0 BIOS_FW_UPDATE_CUT_WAIT_S=2 \
          BIOS_FW_UPDATE_LOCK_DIR="$work" \
          "$work/bin" flash 10.0.0.1 http://share/TPC_P26F.tar --port et25b1 2>"$work/err.$1")
    rc=$?
}
ok()   { pass=$((pass + 1)); echo "ok   $1"; }
bad()  { fail=$((fail + 1)); echo "FAIL $1"; echo "     rc=$rc out=$(echo "$out" | tail -n 3)"; }
posted() { grep -q '^POST /redfish/v1/UpdateService/update' "$FIX_CMDLOG"; }
patched() { grep -q '^PATCH' "$FIX_CMDLOG"; }

now=$(date +%s)
printf 'Completed 100\n' > "$work/seq.done"
export FIX_SEQ="$work/seq.done" FIX_OLD_TASK='{}' FIX_JOURNAL=/dev/null

# ── a/b: the interlock ───────────────────────────────────────────────────────
FIX_TASKS_COLL='{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}' \
FIX_OLD_TASK='{"Id":"1","TaskState":"Running","PercentComplete":40}' run running
[ $rc -eq 3 ] && ! posted && echo "$out" | grep -q '"reason": "job_running"' && ok "running job -> busy, no push" || bad "running job"

FIX_TASKS_COLL='{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}' \
FIX_OLD_TASK="{\"Id\":\"1\",\"TaskState\":\"Completed\",\"EndTime\":\"$(iso $((now - 30)))\"}" run quiet
[ $rc -eq 3 ] && ! posted && echo "$out" | grep -q '"reason": "quiet_window"' && ok "job ended 30s ago -> quiet window, no push" || bad "quiet window"

FIX_TASKS_COLL='{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}' \
FIX_OLD_TASK="{\"Id\":\"1\",\"TaskState\":\"Completed\",\"EndTime\":\"$(iso $((now - 120)))\"}" run pastquiet
[ $rc -eq 0 ] && posted && ok "job ended 120s ago -> pushes" || bad "past quiet window"

FIX_TASKS_CODE=500 run unreadable
[ $rc -eq 3 ] && ! posted && echo "$out" | grep -q tasks_unreadable && ok "task list unreadable -> busy (fail closed), no push" || bad "unreadable"

FIX_POST_CODE=423 run r423
[ $rc -eq 3 ] && ! patched && echo "$out" | grep -q http_423 && ok "423 -> busy, NO busy-flag PATCH, no retry" || bad "423"
[ "$(grep -c '^POST' "$work/cmd.r423")" -eq 1 ] && ok "423 -> exactly one POST" || { rc=x; bad "423 single POST"; }

exec 9>"$work/fw-update-10.0.0.1.lock"; flock -n 9
run locked
[ $rc -eq 3 ] && ! posted && echo "$out" | grep -q local_lock && ok "local lock held -> busy, no push" || bad "local lock"
exec 9>&-

# ── c: verdict classification ────────────────────────────────────────────────
cat > "$work/j.booted" <<'J'
2026-09-24T12:31:40+00:00 tiogapass bios-update[1]: bios-update: ME region is unchanged — flashcp -p will not touch it, no power cycle needed
2026-09-24T12:33:40+00:00 tiogapass bios-update[2]: diff blocks: 70
2026-09-24T12:33:41+00:00 tiogapass bios-update[1]: bios-update: flash complete
2026-09-24T12:34:50+00:00 tiogapass bios-update[1]: bios-update: host state after power-on: xyz.openbmc_project.State.Host.HostState.Running
J
FIX_JOURNAL="$work/j.booted" run booted
v=$(echo "$out" | grep '"phase": "verdict"')
[ $rc -eq 0 ] && echo "$v" | grep -q '"ending": "booted"' && echo "$v" | grep -q '"diff_blocks": 70' && ok "ME unchanged + host up -> booted, diff_blocks read" || bad "booted verdict"

cat > "$work/j.pof" <<'J'
2026-09-24T12:31:40+00:00 tiogapass bios-update[1]: bios-update: ME region is unchanged — flashcp -p will not touch it, no power cycle needed
2026-09-24T12:33:41+00:00 tiogapass bios-update[1]: bios-update: flash complete
2026-09-24T12:34:50+00:00 tiogapass bios-update[1]: bios-update: host state after power-on: xyz.openbmc_project.State.Host.HostState.Off
2026-09-24T12:34:50+00:00 tiogapass bios-update[1]: bios-update: host did not come up, and the ME was NOT rewritten — this is not the post-ME-flash lockout.
J
FIX_JOURNAL="$work/j.pof" run pof
echo "$out" | grep -q '"ending": "poweron_failed"' && ok "ME unchanged + host Off -> poweron_failed" || bad "poweron_failed verdict"

FIX_JOURNAL=/dev/null run nolines
echo "$out" | grep -q '"ending": "unknown"' && [ $rc -eq 0 ] && ok "journal empty/rolled -> unknown, not an error" || bad "unknown verdict"

# ── d: the ME-changed cut check ──────────────────────────────────────────────
cat > "$work/j.me" <<'J'
2026-09-24T18:02:17+00:00 tiogapass bios-update[1]: bios-update: ME region DIFFERS — the node must go through G3 after the flash or it will not power on
2026-09-24T18:02:36+00:00 tiogapass bios-update[2]: diff blocks: 8
2026-09-24T18:02:36+00:00 tiogapass bios-update[1]: bios-update: flash complete
2026-09-24T18:02:41+00:00 tiogapass bios-update[1]: bios-update: scheduling node power cycle in 20s (i2cset -f -y 7 0x45 0xd9 0x0c)
J
FIX_JOURNAL="$work/j.me" FIX_BOOT_SAME_FOR=999 run cutmissing
echo "$out" | grep -q '"ending": "me_changed_cycle_scheduled"' && echo "$out" | grep -q '"cut": "cut_missing"' \
  && ok "ME changed, same boot_id -> cut_missing (the et26b3 case)" || bad "cut_missing"

FIX_JOURNAL="$work/j.me" FIX_BOOT_SAME_FOR=1 FIX_BOOT_AFTER=DOWN run cutdown
echo "$out" | grep -q '"cut": "bmc_dropped"' && ok "ME changed, BMC stops answering -> bmc_dropped" || bad "bmc_dropped"

FIX_JOURNAL="$work/j.me" FIX_BOOT_SAME_FOR=1 FIX_BOOT_AFTER=cccc run cutreboot
echo "$out" | grep -q '"cut": "bmc_rebooted"' && ok "ME changed, new boot_id -> bmc_rebooted" || bad "bmc_rebooted"

# ── task failures ────────────────────────────────────────────────────────────
printf 'Running 20\nException 20\n' > "$work/seq.exc"
FIX_SEQ="$work/seq.exc" run exc
[ $rc -eq 1 ] && ok "task Exception -> exit 1" || bad "task exception"

FIX_FETCH_FAIL=1 run nofetch
[ $rc -eq 1 ] && ! posted && ok "artifact fetch failure -> exit 1, no push" || bad "fetch failure"

# ── e: the credential never reaches argv ─────────────────────────────────────
if grep -n 'test-dummy' "$work"/cmd.* >/dev/null 2>&1; then rc=x; bad "credential appeared in a Redfish call's arguments"
else ok "credential absent from every recorded call"; fi
refs=$(grep -n 'SSHPASS' "$work/bin" | grep -vE '^\s*[0-9]+:\s*#' | grep -vE 'export SSHPASS=|printf .machine %s login %s password %s|"\$SSHPASS"\)' )
[ -z "$refs" ] && ok "SSHPASS used only via export, netrc printf and sshpass -e" || { rc=x; out="$refs"; bad "unexpected SSHPASS use"; }


# ── the lock directory: sudo re-exec, never a fallback dir (2026-09-24) ─────
rodir="$work/ro-lockdir"; mkdir -p "$rodir"; chmod 555 "$rodir"
printf '#!/bin/bash\nexit 1\n' > "$work/nosudo"
printf '#!/bin/bash\n[ "$1" = "-n" ] && [ "$2" = "true" ] && exit 0\nprintf "%%s\\n" "$*" > "$FIX_SUDOLOG"\nexit 0\n' > "$work/fakesudo"
chmod +x "$work/nosudo" "$work/fakesudo"
if [ "$(id -u)" != 0 ]; then
    : > "$work/cmd.nosudo"
    o=$(env FIX_CMDLOG="$work/cmd.nosudo" FLAX_SUDO="$work/nosudo" BIOS_FW_UPDATE_LOCK_DIR="$rodir" FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/ssh" FLAX_FETCH_EXEC="$work/fetch" \
        "$work/bin" flash 10.0.0.1 http://share/TPC_P26F.tar 2>&1); r=$?
    if [ $r -eq 2 ] && [[ "$o" == *"cannot write the lock directory"* ]] && ! grep -qE '^(POST|RF POST)|i2cset' "$work/cmd.nosudo"; then
        echo "ok   - unwritable lock dir, no sudo -> exit 2, nothing sent"; pass=$((pass+1))
    else echo "FAIL - unwritable lock dir, no sudo (rc=$r out=$o)"; fail=$((fail+1)); fi
    export FIX_SUDOLOG="$work/sudolog"; : > "$FIX_SUDOLOG"
    env FLAX_SUDO="$work/fakesudo" BIOS_FW_UPDATE_LOCK_DIR="$rodir" FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/ssh" FLAX_FETCH_EXEC="$work/fetch" "$work/bin" flash 10.0.0.1 http://share/TPC_P26F.tar >/dev/null 2>&1; r=$?
    if [ $r -eq 0 ] && grep -q -- "-n $work/bin flash 10.0.0.1 http://share/TPC_P26F.tar" "$FIX_SUDOLOG"; then
        echo "ok   - unwritable lock dir -> re-runs itself under sudo -n with the same args"; pass=$((pass+1))
    else echo "FAIL - sudo re-exec (rc=$r log=$(cat "$FIX_SUDOLOG"))"; fail=$((fail+1)); fi
else
    echo "ok   - (skipped sudo re-exec cases: running as root)"; pass=$((pass+1))
fi
chmod 755 "$rodir"

echo "---"; echo "pass=$pass fail=$fail"
[ $fail -eq 0 ]
