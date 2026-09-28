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
  *journalctl*)
      [ -n "${FIX_SSH_DOWN:-}" ] && exit 255
      cat "$FIX_JOURNAL"
      case "$2" in *JOURNAL_END*) echo JOURNAL_END ;; esac ;;
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
refs=$(grep -n 'SSHPASS' "$work/bin" | grep -vE '^\s*[0-9]+:\s*#' | grep -vE 'export SSHPASS=|login \$RF_USER password \$SSHPASS"|"\$SSHPASS"\)' )
[ -z "$refs" ] && ok "SSHPASS used only via export, the netrc heredoc and sshpass -e" || { rc=x; out="$refs"; bad "unexpected SSHPASS use"; }


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

# ── journal subcommand ────────────────────────────────────────────────────
jrun() {  # jrun <name> -- runs `journal`, sets $out $rc
    export FIX_CMDLOG="$work/cmd.$1"; : > "$FIX_CMDLOG"
    out=$(FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/ssh" \
          "$work/bin" journal 10.0.0.1 1790000000 2>"$work/err.$1"); rc=$?
}
cat > "$work/j.mixed" <<'J'
2026-09-24T18:03:01+00:00 bmc bios-update[812]: ME region is unchanged
2026-09-24T18:03:02+00:00 bmc power-control[301]: PowerControl: power supply power good failed to assert
2026-09-24T18:03:03+00:00 bmc entity-manager[455]: terminate called after throwing an instance of 'std::runtime_error'
2026-09-24T18:03:04+00:00 bmc fru-device[456]: JSON file not found /usr/share/entity-manager/configurations/eeprom.json
2026-09-24T18:03:05+00:00 bmc systemd[1]: Started Something unrelated
EM_INVENTORY
/xyz/openbmc_project/inventory/system/board
/xyz/openbmc_project/inventory/system/board/Cpld
/xyz/openbmc_project/inventory/system/board/TiogaPass_Baseboard
J
FIX_JOURNAL="$work/j.mixed" jrun jmixed
if [ $rc -eq 0 ] \
   && echo "$out" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["phase"]=="journal"; assert len(d["bios_update"])==1; assert len(d["power_control"])==1; assert len(d["entity_manager"])==2; assert d["em_inventory"]==["board","board/Cpld","board/TiogaPass_Baseboard"]; assert not any("unrelated" in l for k in ("bios_update","power_control","entity_manager") for l in d[k])'; then
    ok "journal splits bios-update / power-control / entity-manager lines, drops the rest"
else bad "journal splits bios-update / power-control / entity-manager lines, drops the rest"; fi

: > "$work/j.empty"
FIX_JOURNAL="$work/j.empty" jrun jempty
if [ $rc -eq 0 ] && echo "$out" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["power_control"]==[] and d["entity_manager"]==[]'; then
    ok "journal with no matching lines is an empty record, exit 0 (not unreachable)"
else bad "journal with no matching lines is an empty record, exit 0 (not unreachable)"; fi

FIX_SSH_DOWN=1 FIX_JOURNAL="$work/j.empty" jrun jdown
if [ $rc -eq 1 ] && [ "$out" = '{"error":"ssh_unreachable"}' ]; then
    ok "journal on a dead ssh is ssh_unreachable, exit 1"
else bad "journal on a dead ssh is ssh_unreachable, exit 1"; fi

# the flash verdict carries the same lists
FIX_JOURNAL="$work/j.mixed"   # reuse the flash fixture shape: completed task, ME unchanged
printf '2026-09-24T18:03:06+00:00 bmc bios-update[812]: host state after power-on: Off\n' >> "$work/j.mixed"
run vlists
v=$(echo "$out" | grep '"phase": *"verdict"')
if [ $rc -eq 0 ] && echo "$v" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert len(d["power_control"])==1; assert len(d["entity_manager"])==2; assert all("bios-update[" in l for l in d["lines"])'; then
    ok "flash verdict carries power_control / entity_manager; verdict lines stay bios-update only"
else bad "flash verdict carries power_control / entity_manager; verdict lines stay bios-update only"; fi

# ── F5/F6: bounded verdict read, no orphaned children (2026-09-27, et6b4) ────

# --- F5: a hung verdict read is bounded, and the flash still reports ---
cat > "$work/ssh.hang" <<'EOF'
#!/bin/bash
case "$2" in
  *journalctl*) sleep 30 ;;              # the et6b4 hang: journal/busctl never returns
  *) exec "$REAL_SSH_STUB" "$@" ;;
esac
EOF
chmod +x "$work/ssh.hang"
printf 'Completed 100\n' > "$work/seq.ok"
export FIX_CMDLOG="$work/cmd.hang" FIX_SEQN="$work/seqn.hang" FIX_BOOTN="$work/bootn.hang"
: > "$FIX_CMDLOG"; rm -f "$FIX_SEQN" "$FIX_BOOTN"
t0=$(date +%s)
out=$(REAL_SSH_STUB="$work/ssh" FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/ssh.hang" FLAX_FETCH_EXEC="$work/fetch" \
      BIOS_FW_UPDATE_POLL_S=0 BIOS_FW_UPDATE_CUT_POLL_S=0 BIOS_FW_UPDATE_CUT_WAIT_S=2 \
      BIOS_FW_UPDATE_LOCK_DIR="$work" BIOS_FW_UPDATE_JOURNAL_FETCH_S=2 \
      FIX_SEQ="$work/seq.ok" \
      "$work/bin" flash 10.0.0.1 http://share/TPC_P26F.tar --port et25b1 2>"$work/err.hang")
rc=$?
dt=$(( $(date +%s) - t0 ))
[ "$dt" -lt 20 ] && echo "$out" | grep -q '"phase": "verdict"' && echo "$out" | grep -q '"ending": "unknown"' \
  && ok "hung journal read is cut at JOURNAL_FETCH_S and the verdict is still printed" || bad "hung verdict read ($dt s)"

# --- F6 (fix round 2, 2026-09-27): the bin now runs as a PARENT (holds
#     fd 9, the pid callers see) that re-execs its body as a CHILD
#     (FLAX_BIN_CHILD=1, via `setpriv --pdeathsig KILL`) which never sees
#     fd 9. Round 1's F6 hung the WHOLE process tree and asserted nothing
#     survived AND the lock was free -- but round 1 turned out to leave
#     the CHILD (a subshell of the SAME process there, not a real child)
#     alive and holding the lock on a SIGKILL of just the bin's own pid,
#     which for bmc-blade-power-cycle meant a 12 V cut was sent AFTER the
#     caller gave up (N2). These SIGKILL the TOP-LEVEL PID ONLY (never the
#     whole tree) and check the PARENT/CHILD split specifically. A
#     distinctive sleep duration (not used anywhere else in this suite) is
#     the pgrep fingerprint.
hang_after_lock_setup() {  # hang_after_lock_setup <sleep-duration> <stub-filename>
    local dur="$1" stubname="$2"
    cat > "$work/$stubname" <<EOF
#!/bin/bash
case "\$2" in
  *journalctl*) exec sleep $dur ;;
  *) exec "\$REAL_SSH_STUB" "\$@" ;;
esac
EOF
    chmod +x "$work/$stubname"
}
sleep_fp() { echo "$1" | sed 's/\./\\./g'; }   # fixture duration -> pgrep -f pattern

# --- SIGTERM case (unchanged behaviour from round 1, re-verified here) ---
hang_after_lock_setup 61.409 ssh.hangterm
printf 'Completed 100\n' > "$work/seq.hangterm"
lockfile_term="$work/fw-update-10.0.0.1.lock"; rm -f "$lockfile_term"
( REAL_SSH_STUB="$work/ssh" FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/ssh.hangterm" FLAX_FETCH_EXEC="$work/fetch" \
    BIOS_FW_UPDATE_POLL_S=0 BIOS_FW_UPDATE_LOCK_DIR="$work" BIOS_FW_UPDATE_JOURNAL_FETCH_S=30 \
    FIX_CMDLOG="$work/cmd.hangterm" FIX_SEQN="$work/seqn.hangterm" FIX_BOOTN="$work/bootn.hangterm" FIX_SEQ="$work/seq.hangterm" \
    bash "$work/bin" flash 10.0.0.1 http://share/TPC_P26F.tar --port et25b1 >/dev/null 2>"$work/err.hangterm" ) &
bpid=$!
for i in $(seq 1 100); do pgrep -f 'sleep 61\.409' >/dev/null 2>&1 && break; sleep 0.1; done
pgrep -f 'sleep 61\.409' >/dev/null 2>&1 || { rc=x; bad "F6 SIGTERM setup: the hang never started"; }
t0=$(date +%s)
kill -TERM "$bpid"
wait "$bpid" 2>/dev/null
dt=$(( $(date +%s) - t0 ))
survivor=$(pgrep -f 'sleep 61\.409' 2>/dev/null)
if [ "$dt" -le 5 ] && [ -z "$survivor" ]; then
    ok "SIGTERM while hung in journal_fetch AFTER take_lock exits fast (${dt}s), no survivor"
else
    rc=x; bad "SIGTERM post-take_lock hang (dt=${dt}s survivor=$survivor)"
fi
if flock -n "$lockfile_term" true; then
    ok "lock free after SIGTERM (post-take_lock hang)"
else
    rc=x; bad "lock still held after SIGTERM (post-take_lock hang)"
fi

# --- SIGKILL of the TOP-LEVEL PID ONLY (N2): on_signal never runs
#     (uncatchable), so the ONLY defences are --pdeathsig KILL (the CHILD
#     dies the instant the PARENT does) and fd 9 never being inherited by
#     anything but the PARENT. ---
hang_after_lock_setup 62.583 ssh.hangkill
printf 'Completed 100\n' > "$work/seq.hangkill"
lockfile_kill="$work/fw-update-10.0.0.1.lock"; rm -f "$lockfile_kill"
( REAL_SSH_STUB="$work/ssh" FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/ssh.hangkill" FLAX_FETCH_EXEC="$work/fetch" \
    BIOS_FW_UPDATE_POLL_S=0 BIOS_FW_UPDATE_LOCK_DIR="$work" BIOS_FW_UPDATE_JOURNAL_FETCH_S=30 \
    FIX_CMDLOG="$work/cmd.hangkill" FIX_SEQN="$work/seqn.hangkill" FIX_BOOTN="$work/bootn.hangkill" FIX_SEQ="$work/seq.hangkill" \
    bash "$work/bin" flash 10.0.0.1 http://share/TPC_P26F.tar --port et25b1 >/dev/null 2>"$work/err.hangkill" ) &
bpid=$!
for i in $(seq 1 100); do pgrep -f 'sleep 62\.583' >/dev/null 2>&1 && break; sleep 0.1; done
pgrep -f 'sleep 62\.583' >/dev/null 2>&1 || { rc=x; bad "F6 SIGKILL setup: the hang never started"; }
childpid_kill=""
for i in $(seq 1 50); do childpid_kill=$(pgrep -P "$bpid" 2>/dev/null | head -1); [ -n "$childpid_kill" ] && break; sleep 0.1; done
t0=$(date +%s)
kill -9 "$bpid"
child_gone=0
for i in $(seq 1 20); do
    kill -0 "$childpid_kill" 2>/dev/null || { child_gone=1; break; }
    sleep 0.1
done
dt=$(( $(date +%s) - t0 ))
if [ "$child_gone" = 1 ] && [ "$dt" -le 3 ]; then
    ok "SIGKILL of the top-level pid: the child is gone within ${dt}s (--pdeathsig KILL)"
else
    rc=x; bad "SIGKILL of the top-level pid: child $childpid_kill still alive after ${dt}s"
fi
if flock -n "$lockfile_kill" true; then
    ok "lock free immediately after SIGKILL of the top-level pid"
else
    rc=x; bad "lock still held after SIGKILL of the top-level pid"
fi
holder=$(for p in /proc/[0-9]*; do pid=${p#/proc/}; [ -e "$p/fd/9" ] || continue; tgt=$(readlink "$p/fd/9" 2>/dev/null); [ "$tgt" = "$lockfile_kill" ] && echo "$pid"; done)
if [ -z "$holder" ]; then
    ok "no surviving process holds fd 9 on the lock file after SIGKILL"
else
    rc=x; bad "fd 9 still held by:$holder"
fi
pkill -9 -f 'sleep 62\.583' 2>/dev/null

# --- N1: TERM (then KILL) sent to the CHILD directly, bypassing the
#     parent entirely (e.g. OOM, pkill, a supervisor that targets the
#     child). Round 1's `while [ "$rc" -gt 128 ]` loop spun at 100% CPU
#     forever here, because bash keeps returning the SAME saved exit
#     status for an already-reaped pid; `kill -0` on the child is what
#     actually tells "wait was interrupted, child still alive" apart from
#     "the child is truly gone" (fix round 2, N1). Each case is bounded by
#     its own poll loop, not a blocking `wait`, so a reintroduced spin
#     fails this test instead of hanging the suite. ---
kill_child_test() {  # kill_child_test <signal-name> <sleep-duration> <stub-filename> <poll-cap-x0.1s>
    # NOTE on the TERM case's short (~2s) hang: the CHILD's own TERM/INT/HUP
    # trap (which removes $ART) is itself subject to the SAME bash rule
    # that motivated this whole fix round -- a trap is deferred while the
    # process is blocked in a FOREGROUND command substitution, and here
    # that process IS the child, blocked in journal_fetch's own `out=$(...)`.
    # kill_tree (used when the PARENT is signaled) works around this by
    # freezing and killing the child's descendants FIRST, so by the time
    # the child itself is signaled it is no longer blocked on anything --
    # but a signal sent DIRECTLY to the child, bypassing kill_tree entirely,
    # has no such help. A short fixture hang lets it resolve on its own
    # within the poll window, so the pending TERM is processed the moment
    # the child is next between foreground commands -- this is what
    # realistically bounds "OOM/pkill/a supervisor signals the child", not
    # "an arbitrary remote hang is instantly interruptible by a raw signal
    # to an inner process", which no shell can promise. SIGKILL needs none
    # of this (uncatchable, kills the instant it arrives regardless of what
    # the child is blocked on), so it keeps a long fixture hang to prove
    # that independence.
    local sig="$1" dur="$2" stubname="$3" cap="$4" fp; fp=$(sleep_fp "$dur")
    hang_after_lock_setup "$dur" "$stubname"
    printf 'Completed 100\n' > "$work/seq.$stubname"
    ( REAL_SSH_STUB="$work/ssh" FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/$stubname" FLAX_FETCH_EXEC="$work/fetch" \
        BIOS_FW_UPDATE_POLL_S=0 BIOS_FW_UPDATE_LOCK_DIR="$work" BIOS_FW_UPDATE_JOURNAL_FETCH_S=30 \
        FIX_CMDLOG="$work/cmd.$stubname" FIX_SEQN="$work/seqn.$stubname" FIX_BOOTN="$work/bootn.$stubname" FIX_SEQ="$work/seq.$stubname" \
        bash "$work/bin" flash 10.0.0.1 http://share/TPC_P26F.tar --port et25b1 >/dev/null 2>"$work/err.$stubname" ) &
    local bp=$!
    local i childp=""
    for i in $(seq 1 100); do pgrep -f "sleep $fp" >/dev/null 2>&1 && break; sleep 0.1; done
    for i in $(seq 1 50); do childp=$(pgrep -P "$bp" 2>/dev/null | head -1); [ -n "$childp" ] && break; sleep 0.1; done
    if [ -z "$childp" ]; then rc=x; bad "kill-child-$sig: could not find the child pid"; kill -9 "$bp" 2>/dev/null; return; fi
    kill -s "$sig" "$childp"
    local t0 dt still_alive=1
    t0=$(date +%s)
    for i in $(seq 1 "$cap"); do
        kill -0 "$bp" 2>/dev/null || { still_alive=0; break; }
        sleep 0.1
    done
    dt=$(( $(date +%s) - t0 ))
    if [ "$still_alive" = 1 ]; then
        rc=x; bad "kill-child-$sig: parent still alive after ${dt}s (N1 spin?)"
        kill -9 "$bp" "$childp" 2>/dev/null
    else
        wait "$bp" 2>/dev/null; rc=$?
        if [ "$rc" -ne 0 ]; then
            ok "kill-child-$sig: parent exits within ${dt}s, non-zero status ($rc)"
        else
            bad "kill-child-$sig: parent exited with status 0 (unexpected)"
        fi
    fi
    pkill -9 -f "sleep $fp" 2>/dev/null
}
kill_child_test TERM 2.113 ssh.killchildterm 60
kill_child_test KILL 64.782 ssh.killchildkill 40

# --- N1: a downstream reader closing stdout early must not spin the
#     parent either (the CHILD dies of SIGPIPE, same "child died on its
#     own, unrelated to a signal THIS process received" shape as the
#     kill-child cases above). A normal (non-hung) flash run is piped
#     through a 1-byte reader via process substitution, so $! still
#     tracks the bin's own pid (a plain `| head -c1` would make $! the
#     LAST pipeline stage instead). ---
printf 'Completed 100\n' > "$work/seq.closedout"
: > "$work/cmd.closedout"
FLAX_REDFISH_EXEC="$work/rf" FLAX_BMC_REMOTE_EXEC="$work/ssh" FLAX_FETCH_EXEC="$work/fetch" \
  BIOS_FW_UPDATE_POLL_S=0 BIOS_FW_UPDATE_LOCK_DIR="$work" \
  FIX_CMDLOG="$work/cmd.closedout" FIX_SEQN="$work/seqn.closedout" FIX_BOOTN="$work/bootn.closedout" FIX_SEQ="$work/seq.closedout" \
  "$work/bin" flash 10.0.0.1 http://share/TPC_P26F.tar --port et25b1 > >(head -c1 >/dev/null) 2>/dev/null &
bpid=$!
t0=$(date +%s)
still_alive=1
for i in $(seq 1 40); do
    kill -0 "$bpid" 2>/dev/null || { still_alive=0; break; }
    sleep 0.1
done
dt=$(( $(date +%s) - t0 ))
if [ "$still_alive" = 1 ]; then
    rc=x; bad "closed stdout: parent still alive after ${dt}s (N1 spin?)"
    kill -9 "$bpid" 2>/dev/null
else
    ok "closed stdout: parent exits promptly (${dt}s), no spin"
fi
wait "$bpid" 2>/dev/null

# --- N3: rc 3 (interlock busy, either the local lock or the BMC's own
#     busy_check) and rc 2 (usage) must leave no $ART file -- round 1
#     created $ART unconditionally for every `flash` call, before the
#     interlock was even checked, so a routinely-busy BMC (bios_fw polls
#     these) leaked one tmp file per call. ---
mkdir -p "$work/tmphome"
: > "$work/cmd.rc3lock"
exec 8>"$work/fw-update-10.0.0.5.lock"; flock -n 8
out=$(TMPDIR="$work/tmphome" FLAX_REDFISH_EXEC="$work/rf" BIOS_FW_UPDATE_LOCK_DIR="$work" \
      FIX_CMDLOG="$work/cmd.rc3lock" \
      "$work/bin" flash 10.0.0.5 http://share/TPC_P26F.tar 2>&1)
rc=$?
exec 8>&-
nleft=$(find "$work/tmphome" -maxdepth 1 -name 'bios-fw-*.tar' 2>/dev/null | wc -l)
if [ "$rc" -eq 3 ] && [ "$nleft" -eq 0 ]; then ok "rc 3 (local lock) leaves no \$ART file"
else rc=x; bad "rc 3 local lock ART leak (rc=$rc nleft=$nleft)"; fi

: > "$work/cmd.rc3busy"
out=$(TMPDIR="$work/tmphome" FLAX_REDFISH_EXEC="$work/rf" BIOS_FW_UPDATE_LOCK_DIR="$work" \
      FIX_CMDLOG="$work/cmd.rc3busy" \
      FIX_TASKS_COLL='{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}' \
      FIX_OLD_TASK='{"Id":"1","TaskState":"Running","PercentComplete":40}' \
      "$work/bin" flash 10.0.0.6 http://share/TPC_P26F.tar 2>&1)
rc=$?
nleft=$(find "$work/tmphome" -maxdepth 1 -name 'bios-fw-*.tar' 2>/dev/null | wc -l)
if [ "$rc" -eq 3 ] && [ "$nleft" -eq 0 ]; then ok "rc 3 (BMC busy_check) leaves no \$ART file"
else rc=x; bad "rc 3 busy_check ART leak (rc=$rc nleft=$nleft)"; fi

: > "$work/cmd.rc2usage"
out=$(TMPDIR="$work/tmphome" FLAX_REDFISH_EXEC="$work/rf" BIOS_FW_UPDATE_LOCK_DIR="$work" \
      FIX_CMDLOG="$work/cmd.rc2usage" "$work/bin" flash 2>&1)
rc=$?
nleft=$(find "$work/tmphome" -maxdepth 1 -name 'bios-fw-*.tar' 2>/dev/null | wc -l)
if [ "$rc" -eq 2 ] && [ "$nleft" -eq 0 ]; then ok "rc 2 (usage) leaves no \$ART file"
else rc=x; bad "rc 2 usage ART leak (rc=$rc nleft=$nleft)"; fi

echo "---"; echo "pass=$pass fail=$fail"
[ $fail -eq 0 ]
