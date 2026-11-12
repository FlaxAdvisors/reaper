#!/bin/bash
# Tests for bmc-backup-flash: the version reader and netreset.
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
    -e "s#/proc/mtd#$FIX_PROCMTD#g" \
    -e "s#/etc/systemd/network/00-bmc-eth0.network#$FIX_NETFILE#g")
eval "$cmd"
STUB
chmod +x "$work/stub"

mk_chip() { printf 'JUNKJUNKJUNK\n        "machine": "tiogapass",\n        "version": "%s"\nMOREJUNK\n' "$1" > "$2"; }
mk_blank() { head -c 4096 /dev/zero | tr '\0' 'Z' > "$1"; }

net_absent() { rm -f "$work/netfile"; }
net_pinned() { printf '[Match]\nName=eth0\n[Link]\nMACAddress=%s\n[Neighbor]\nMACAddress=aa:aa:aa:aa:aa:aa\n' "$1" > "$work/netfile"; touch -d @1790000000 "$work/netfile"; }
net_unpinned() { printf '[Match]\nName=eth0\n[Neighbor]\nMACAddress=aa:aa:aa:aa:aa:aa\n' > "$work/netfile"; touch -d @1790000000 "$work/netfile"; }
net_absent

run_case() {  # name active_os mtd0_ver mtd5_ver expect_substring
    local name="$1" osver="$2" m0="$3" m5="$4" want="$5"
    printf 'VERSION_ID=%s\n' "$osver" > "$work/osrel"
    if [ "$m0" = "BLANK" ]; then mk_blank "$work/m0"; else mk_chip "$m0" "$work/m0"; fi
    if [ "$m5" = "BLANK" ]; then mk_blank "$work/m5"; else mk_chip "$m5" "$work/m5"; fi
    printf 'dev:    size   erasesize  name\nmtd0: 04000000 00010000 "bmc"\nmtd5: 04000000 00010000 "bmc-backup"\n' > "$work/procmtd"
    local out
    out=$(FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_MTD0="$work/m0" FIX_MTD5="$work/m5" \
          FIX_OSREL="$work/osrel" FIX_PROCMTD="$work/procmtd" FIX_NETFILE="$work/netfile" \
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

echo "bmc-backup-flash version: network file"
net_absent
run_case "no network file" "flax-onetree-1.1.2" "flax-onetree-1.1.2" "flax-onetree-1.1.2" \
    '"net_file":"absent","net_pin":"none","net_mtime":null'
net_pinned "98:03:9b:a8:f3:f0"
run_case "pinned network file: [Link] MAC, not [Neighbor]" "flax-onetree-1.1.2" "flax-onetree-1.1.2" "flax-onetree-1.1.2" \
    '"net_file":"present","net_pin":"98:03:9b:a8:f3:f0","net_mtime":1790000000'
net_unpinned
run_case "file without a [Link] pin" "flax-onetree-1.1.2" "flax-onetree-1.1.2" "flax-onetree-1.1.2" \
    '"net_file":"present","net_pin":"none","net_mtime":1790000000'
net_pinned "98:03:9b:a8:f3:f0"
run_case "net fields ride the canary-failure record too" "flax-onetree-1.1.1" "flax-onetree-9.9.9" "flax-onetree-1.1.1" \
    '"error":"reader_canary_failed","net_file":"present"'
net_absent
run_case "backup_version record unchanged by the net fields" "flax-onetree-1.1.1" "flax-onetree-1.1.1" "flax-onetree-1.1.1-202608281924" \
    '"backup_version":"flax-onetree-1.1.1-202608281924","active_version":"flax-onetree-1.1.1","net_file":"absent"'

# A chip holding Facebook OpenBMC is a readable chip with a version that is
# not ours -- not a blank and not "unreadable" (et8b3 / et24b1 2026-10-02).
mk_fb() { printf 'JUNKJUNKJUNK\nU-Boot SPL 2016.07 %s (Oct 31 2019 - 00:50:32)\nU-Boot fitImage for Facebook OpenBMC/1.0/fbtp\nMOREJUNK\n' "$1" > "$2"; }
fb_case() {  # name fb_version expect_substring
    printf 'VERSION_ID=%s\n' "flax-onetree-1.1.4" > "$work/osrel"
    mk_chip "flax-onetree-1.1.4" "$work/m0"; mk_fb "$2" "$work/m5"
    printf 'dev:    size   erasesize  name\nmtd0: 04000000 00010000 "bmc"\nmtd5: 04000000 00010000 "bmc-backup"\n' > "$work/procmtd"
    local out
    out=$(FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_MTD0="$work/m0" FIX_MTD5="$work/m5" \
          FIX_OSREL="$work/osrel" FIX_PROCMTD="$work/procmtd" FIX_NETFILE="$work/netfile" \
          "$work/bin" version 10.0.0.1 2>&1)
    if printf '%s' "$out" | grep -qF "$3" && ! printf '%s' "$out" | grep -q '"error"'; then
        printf '  PASS  %s\n' "$1"; pass=$((pass+1))
    else
        printf '  FAIL  %s\n        want substring: %s\n        got: %s\n' "$1" "$3" "$out"; fail=$((fail+1))
    fi
}
fb_case "Facebook OpenBMC on the backup chip reads as its own version" \
    "fbtp-v2019.43.0" '"backup_version":"fbtp-v2019.43.0"'
fb_case "old Facebook OpenBMC (fbtp-v4.2) reads too" \
    "fbtp-v4.2" '"backup_version":"fbtp-v4.2"'
# Presence proof for the blank case: a chip with neither string still reports
# read_failed, so the Facebook match did not turn every chip into a version.
run_case "a blank backup chip is still read_failed" \
    "flax-onetree-1.1.1" "flax-onetree-1.1.1" "BLANK" \
    '"error":"read_failed"'

# ── write: chip size and a chip that takes nothing ───────────────────────────
echo "bmc-backup-flash write"
# The write stub answers the BMC-side flash script from a fixture and runs
# everything else (the /proc/mtd size read) for real; scp is a no-op seam.
cat > "$work/wstub" <<'STUB'
#!/bin/bash
case "$2" in
    *backup-bmc-flash*) printf '%s\nRC=%s\n' "$FIX_FLASH_OUT" "${FIX_FLASH_RC:-0}"; exit 0 ;;
    *"rm -f /tmp/image-bmc"*) exit 0 ;;
esac
cmd=$(printf '%s' "$2" | sed -e "s#/proc/mtd#$FIX_PROCMTD#g")
eval "$cmd"
STUB
cat > "$work/scpstub" <<'STUB'
#!/bin/bash
echo "$1 $2" >> "$FIX_SCP_LOG"
STUB
chmod +x "$work/wstub" "$work/scpstub"
write_case() {  # name chip_hex image_bytes flash_out flash_rc expect_substring scp_expected(yes|no)
    local name="$1" chip="$2" bytes="$3" fout="$4" frc="$5" want="$6" scp="$7" out
    rm -rf "$work/cache"; mkdir -p "$work/cache"; : > "$work/scplog"
    head -c "$bytes" /dev/zero > "$work/cache/img.image-bmc"
    printf '%s\n' "$bytes" > "$work/cache/img.image-bmc.size"
    printf 'dev:    size   erasesize  name\nmtd0: 04000000 00010000 "bmc"\nmtd5: %s 00010000 "bmc-backup"\n' "$chip" > "$work/procmtd"
    out=$(FLAX_BMC_REMOTE_EXEC="$work/wstub" FLAX_BMC_SCP_EXEC="$work/scpstub" FIX_SCP_LOG="$work/scplog" \
          FIX_PROCMTD="$work/procmtd" FIX_FLASH_OUT="$fout" FIX_FLASH_RC="$frc" BMC_BACKUP_CACHE="$work/cache" \
          "$work/bin" write 10.0.0.1 http://bang/x/img.tar 2>&1)
    local scpd=no; [ -s "$work/scplog" ] && scpd=yes
    if printf '%s' "$out" | grep -qF "$want" && [ "$scpd" = "$scp" ]; then
        printf '  PASS  %s\n' "$name"; pass=$((pass+1))
    else
        printf '  FAIL  %s\n        want substring: %s (scp %s)\n        got: %s (scp %s)\n' "$name" "$want" "$scp" "$out" "$scpd"; fail=$((fail+1))
    fi
}
# 0x1000 = 4096-byte chip.
write_case "an image larger than the chip is refused before the scp" \
    00001000 8192 "" 0 '{"error":"chip_too_small","chip_bytes":4096,"image_bytes":8192}' no
write_case "an image that fits is written" \
    00001000 4096 "backup-bmc-flash: done" 0 '{"ok":true}' yes
write_case "an unreadable chip size does not refuse (the BMC script decides)" \
    zz 8192 "backup-bmc-flash: done" 0 '{"ok":true}' yes
write_case "the BMC script's own size refusal maps to chip_too_small" \
    zz 8192 "backup-bmc-flash: error: image (8192 B) is larger than chip (4096 B)" 1 '{"error":"chip_too_small"}' yes
write_case "a chip that takes nothing is write_rejected, not flash_failed" \
    00001000 4096 "File does not seem to match flash data. First mismatch at 0x00000000-0x00010000" 1 '{"error":"write_rejected"}' yes
write_case "any other flash failure is still flash_failed" \
    00001000 4096 "flashcp: something else" 1 '{"error":"flash_failed"}' yes

# ── netreset (Part 3 Task 2) ─────────────────────────────────────────────────
# FactoryReset of the BMC network file + a verified reboot. The stub models a
# BMC across a reboot: state files in $NR_DIR, and the bin's REAL state-read
# command string is executed against them (boot_id, addr_assign_type, the
# network file and `ip link show eth0` rewritten to fixtures) -- the same
# honesty rule as the version reader above.
# final-review fix: a pin with '"' and '\' must not break the version record
printf '[Match]\nName=eth0\n[Link]\nMACAddress=98:03"9b\\a8\r\n' > "$work/netfile"; touch -d @1790000000 "$work/netfile"
printf 'VERSION_ID=flax-onetree-1.1.2\n' > "$work/osrel"; mk_chip flax-onetree-1.1.2 "$work/m0"; mk_chip flax-onetree-1.1.2 "$work/m5"
vout=$(FLAX_BMC_REMOTE_EXEC="$work/stub" FIX_MTD0="$work/m0" FIX_MTD5="$work/m5" FIX_OSREL="$work/osrel" \
       FIX_PROCMTD="$work/procmtd" FIX_NETFILE="$work/netfile" "$work/bin" version 10.0.0.1 2>/dev/null)
if printf '%s' "$vout" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); assert d["backup_version"]=="flax-onetree-1.1.2", d' 2>/dev/null; then
    printf '  PASS  %s\n' "pin with a quote + backslash + CR: version record is valid JSON, backup_version kept"; pass=$((pass+1))
else
    printf '  FAIL  %s\n        got: %s\n' "pin with a quote + backslash + CR: valid JSON" "$vout"; fail=$((fail+1))
fi
net_absent

echo "bmc-backup-flash netreset"
cat > "$work/nrstub" <<'STUB'
#!/bin/bash
# $1 = target (ip or ll%iface), $2 = remote command. BMC state lives in $NR_DIR.
t="$1"; cmd="$2"; d="$NR_DIR"
echo "$t :: $cmd" >> "$d/log"
case "$cmd" in
  *FactoryReset*) [ -f "$d/factoryreset_fails" ] && exit 1
                  [ -n "${NR_RESET_HANG:-}" ] && { sleep "$NR_RESET_HANG"; exit 0; }; rm -f "$d/netfile"; exit 0 ;;
  "[ ! -f "*) [ ! -f "$d/netfile" ]; exit $? ;;
  *"reboot -f"*)
      # ordering proof: was the reboot marker already there when the reboot went out?
      [ -e "$FLAX_REBOOT_DIR/et8b1" ] && echo marker_before_reboot >> "$d/log"
      [ -f "$d/wedge" ] || echo new-boot > "$d/boot_next"
      # NR_REBOOT_HANG: the BMC is gone but the ssh never returns (no FIN/RST)
      [ -n "${NR_REBOOT_HANG:-}" ] && sleep "$NR_REBOOT_HANG"; exit 255 ;;
esac
# the state read. NR_HANG: the FIRST read hangs (signal tests), then answers.
if [ -n "${NR_HANG:-}" ] && [ ! -e "$d/hung" ]; then touch "$d/hung"; sleep "$NR_HANG"; fi
# NR_POLL_HANG: the FIRST post-reboot link-local read hangs, never answers
if [ -f "$d/boot_next" ] && [ -n "${NR_POLL_HANG:-}" ] && [ ! -e "$d/pollhung" ] && case "$t" in fe80::*) true;; *) false;; esac; then
  touch "$d/pollhung"; sleep "$NR_POLL_HANG"; exit 255
fi
if [ -f "$d/boot_next" ]; then
  # after the reboot: NR_LL_ONLY -> the old IPv4 is gone; NR_IP_ONLY -> no LL
  case "$t" in
    fe80::*) [ -n "${NR_IP_ONLY:-}" ] && exit 255 ;;
    *)       [ -n "${NR_LL_ONLY:-}" ] && exit 255 ;;
  esac
  mv "$d/boot_next" "$d/boot"; [ -f "$d/regen" ] && cp "$d/regen" "$d/netfile"
  echo "${NR_ASSIGN_AFTER:-0}" > "$d/assign"; cp "$d/perm" "$d/mac"
fi
# `ip link` prints permaddr only when it differs from the current MAC
{ echo "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000"
  printf '    link/ether %s brd ff:ff:ff:ff:ff:ff' "$(cat "$d/mac")"
  [ "$(cat "$d/perm")" != "$(cat "$d/mac")" ] && printf ' permaddr %s' "$(cat "$d/perm")"; echo; } > "$d/iplink"
cmd=$(printf '%s' "$cmd" | sed \
    -e "s#/proc/sys/kernel/random/boot_id#$d/boot#g" \
    -e "s#/sys/class/net/eth0/addr_assign_type#$d/assign#g" \
    -e "s#/etc/systemd/network/00-bmc-eth0.network#$d/netfile#g" \
    -e "s#ip link show eth0#cat $d/iplink#g" \
    -e "s#busctl get-property xyz.openbmc_project.State.Chassis /xyz/openbmc_project/state/chassis0 xyz.openbmc_project.State.Chassis CurrentPowerState#cat $d/power#g" \
    -e "s#busctl get-property xyz.openbmc_project.PSUSensor /xyz/openbmc_project/sensors/power/MB_VR_CPU0_VCCIN_Output_Power xyz.openbmc_project.Sensor.Value Value#cat $d/cpu0#g")
eval "$cmd"
STUB
chmod +x "$work/nrstub"
# Redfish stub, Part 1's FLAX_REDFISH_EXEC protocol: $1 method, $2 path; body
# then a trailing HTTP=<code> line. RF_TASK = the one task document, if any.
cat > "$work/nrrf" <<'RF'
#!/bin/bash
case "$1 $2" in
  "GET /redfish/v1/TaskService/Tasks")
      if [ -n "${RF_TASK:-}" ]; then printf '{"Members":[{"@odata.id":"/redfish/v1/TaskService/Tasks/1"}]}\nHTTP=200'
      else printf '{"Members":[]}\nHTTP=200'; fi ;;
  "GET /redfish/v1/TaskService/Tasks/1") printf '%s\nHTTP=200' "$RF_TASK" ;;
  *) printf '\nHTTP=404' ;;
esac
RF
chmod +x "$work/nrrf"

nr_setup() {  # nr_setup <pinned-mac> <perm-mac>
    rm -rf "$work/nr"; mkdir -p "$work/nr/lock" "$work/nr/manual" "$work/nr/reboot"
    echo old-boot > "$work/nr/boot"; echo 1 > "$work/nr/assign"; echo "$1" > "$work/nr/mac"; echo "$2" > "$work/nr/perm"
    printf '[Match]\nName=eth0\n[Link]\nMACAddress=%s\n' "$1" > "$work/nr/netfile"
    # CLEANUP.md #8: default the host-power fixture to On so every existing
    # case above (written before the refusal existed) keeps passing; the
    # host-off/unknown cases below override this per-run.
    printf 's "xyz.openbmc_project.State.Chassis.PowerState.On"\n' > "$work/nr/power"
    # ...and CPU0's VR to a live host's idle draw (et23b1 read 5.4375 W).
    printf 'd 5.4375\n' > "$work/nr/cpu0"
    : > "$work/nr/log"
}
nr_pwr_off()     { printf 's "xyz.openbmc_project.State.Chassis.PowerState.Off"\n' > "$work/nr/power"; }
nr_pwr_unknown() { rm -f "$work/nr/power"; }
nr_cpu0()        { printf 'd %s\n' "$1" > "$work/nr/cpu0"; }
nr_cpu0_absent() { rm -f "$work/nr/cpu0"; }
# the test seams (every path under $work). An ARRAY, not only a function: a
# backgrounded function is a subshell, so its $! would not be the bin's pid.
NR_SEAMS=(NR_DIR="$work/nr" FLAX_BMC_REMOTE_EXEC="$work/nrstub" FLAX_REDFISH_EXEC="$work/nrrf"
    BMC_FW_UPDATE_LOCK_DIR="$work/nr/lock" FLAX_MANUAL_CLAIM_DIR="$work/nr/manual" FLAX_REBOOT_DIR="$work/nr/reboot"
    NETRESET_POLL_S=1)
nr_env() { env "${NR_SEAMS[@]}" NETRESET_BOOT_WAIT_S="${WAIT:-6}" "$@"; }
nr_run() {
    nr_env "$work/bin" netreset 172.17.8.101 --port et8b1 "$@" > "$work/nr/out" 2> "$work/nr/err"
    echo $? > "$work/nr/rc"
}
chk() {  # chk <name> <command...>
    local n="$1"; shift
    if "$@"; then printf '  PASS  %s\n' "$n"; pass=$((pass+1))
    else printf '  FAIL  %s\n        rc=%s out=%s err=%s\n' "$n" "$(cat "$work/nr/rc" 2>/dev/null)" "$(cat "$work/nr/out" 2>/dev/null)" "$(tail -3 "$work/nr/err" 2>/dev/null)"; fail=$((fail+1)); fi
}
rc_is()    { [ "$(cat "$work/nr/rc")" = "$1" ]; }
out_has()  { grep -qF -- "$1" "$work/nr/out"; }
err_has()  { grep -qF -- "$1" "$work/nr/err"; }
log_has()  { grep -qF -- "$1" "$work/nr/log"; }
lock_free() { flock -n "$work/nr/lock/fw-update-172.17.8.101.lock" true; }
PIN=98:03:9b:a8:f3:f0; PERM=98:03:9b:a6:fe:f8; LL='fe80::9a03:9bff:fea6:fef8%eth1.17'

nr_setup $PIN $PERM; nr_run
chk "clean: rc 0, verdict clean"              eval 'rc_is 0 && out_has "\"netreset\":\"clean\""'
chk "clean: verdict carries pre/native/post"  out_has "\"pre_mac\":\"$PIN\",\"native_mac\":\"$PERM\",\"pre_pin\":\"$PIN\",\"post_mac\":\"$PERM\",\"post_pin\":\"none\",\"post_assign\":\"0\""
chk "clean: one JSON line on stdout"          eval '[ "$(wc -l < "$work/nr/out")" = 1 ] && python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$work/nr/out"'
chk "clean: exact FactoryReset call sent"     log_has "busctl call xyz.openbmc_project.Network /xyz/openbmc_project/network xyz.openbmc_project.Common.FactoryReset Reset"
chk "clean: reboot is 'sync; reboot -f'"      log_has ":: sync; reboot -f"
chk "reboot marker written"                   test -f "$work/nr/reboot/et8b1"
chk "reboot marker written BEFORE the reboot" log_has marker_before_reboot
chk "claim released on exit"                  test ! -e "$work/nr/manual/et8b1"
chk "lock free after the run"                 lock_free
chk "never calls flax-forget-port"            eval '! log_has forget && ! err_has forget'

# ── host power gate (CLEANUP.md #8 / Persistent_MAC_on_BMC.md) ──────────────
# et8b3, 2026-09-28: netreset's FactoryReset + reboot on a host-off blade left
# the BMC dark until a physical reseat -- a host that is ON is the KCS/IPMI
# safety net if the reset leaves the BMC unreachable on the network. Runs
# after the lock + busy check above, before anything destructive.
nr_setup $PIN $PERM; nr_run
chk "host on (default fixture): proceeds unchanged, clean" eval 'rc_is 0 && out_has "\"netreset\":\"clean\""'

nr_setup $PIN $PERM; nr_pwr_off; nr_run
chk "host off: rc 4, reason host_off"         eval 'rc_is 4 && out_has "\"netreset\":\"refused\",\"reason\":\"host_off\""'
chk "host off: readable refusal on stderr"    err_has "refusing: host is off on 172.17.8.101 -- no KCS safety net"
chk "host off: nothing reset, no marker"      eval '! log_has FactoryReset && ! log_has "reboot -f" && [ ! -e "$work/nr/reboot/et8b1" ]'

nr_setup $PIN $PERM; nr_pwr_unknown; nr_run
chk "host power unreadable: rc 4, reason host_power_unknown" eval 'rc_is 4 && out_has "\"netreset\":\"refused\",\"reason\":\"host_power_unknown\""'
chk "host power unreadable: nothing reset, no marker" eval '! log_has FactoryReset && [ ! -e "$work/nr/reboot/et8b1" ]'

nr_setup $PIN $PERM; nr_pwr_off; nr_run --force
chk "host off + --force: proceeds, clean"     eval 'rc_is 0 && out_has "\"netreset\":\"clean\""'
chk "host off + --force: FactoryReset sent"   log_has FactoryReset

# "On" is not "running": et23b4 (2026-09-28) reads chassis On with CPU0's VR
# at 0.0 W -- a dead host has no KCS either. The host counts as running only
# when CPU0 VCCIN draws >= NETRESET_MIN_CPU0_W (default 1 W).
nr_setup $PIN $PERM; nr_cpu0 0; nr_run
chk "On + CPU0 0 W: rc 4, reason host_not_running" eval 'rc_is 4 && out_has "\"netreset\":\"refused\",\"reason\":\"host_not_running\""'
chk "On + CPU0 0 W: readable refusal on stderr" err_has "refusing: host reads On but CPU0 VR is 0 W on 172.17.8.101 -- no KCS safety net"
chk "On + CPU0 0 W: nothing reset, no marker"  eval '! log_has FactoryReset && ! log_has "reboot -f" && [ ! -e "$work/nr/reboot/et8b1" ]'

nr_setup $PIN $PERM; nr_cpu0 0.03; nr_run
chk "On + CPU0 0.03 W (et8b1): refused host_not_running" eval 'rc_is 4 && out_has "\"reason\":\"host_not_running\""'

nr_setup $PIN $PERM; nr_cpu0 0.5; NETRESET_MIN_CPU0_W=0.4 nr_run
chk "NETRESET_MIN_CPU0_W lowers the floor"     eval 'rc_is 0 && out_has "\"netreset\":\"clean\""'

nr_setup $PIN $PERM; nr_cpu0_absent; nr_run
chk "On + CPU0 sensor absent: rc 4, host_power_unknown" eval 'rc_is 4 && out_has "\"reason\":\"host_power_unknown\""'
chk "On + CPU0 sensor absent: nothing reset"   eval '! log_has FactoryReset'

nr_setup $PIN $PERM; nr_cpu0 0; nr_run --force
chk "On + CPU0 0 W + --force: proceeds, clean" eval 'rc_is 0 && out_has "\"netreset\":\"clean\""'

nr_setup $PIN $PERM; nr_run
chk "status line reports cpu0 watts"          err_has "cpu0=5.4375"

nr_setup $PIN $PERM; NR_LL_ONLY=1 nr_run
chk "native-MAC LL follow: clean via $LL"     eval 'rc_is 0 && out_has "\"target\":\"$LL\""'

nr_setup $PIN $PERM; NR_IP_ONLY=1 nr_run
chk "old IPv4 still tried each poll: clean via 172.17.8.101" eval 'rc_is 0 && out_has "\"target\":\"172.17.8.101\""'

nr_setup $PIN $PERM; NR_LL_ONLY=1 nr_run --iface br-test
chk "--iface overrides the LL parent"         out_has '"target":"fe80::9a03:9bff:fea6:fef8%br-test"'

nr_setup $PERM $PERM; NR_LL_ONLY=1 nr_run
chk "no permaddr: native = current MAC"       eval 'rc_is 0 && out_has "\"native_mac\":\"$PERM\""'

nr_setup $PIN $PERM; printf '[Match]\nName=eth0\n[Link]\nMACAddress=%s\n' $PIN > "$work/nr/regen"; nr_run
chk "file regenerates: rc 3 not_clean"        eval 'rc_is 3 && out_has "\"netreset\":\"not_clean\"" && out_has "\"post_pin\":\"$PIN\""'

nr_setup $PIN $PERM; NR_ASSIGN_AFTER=3 nr_run
chk "addr_assign_type != 0: rc 3 not_clean"   eval 'rc_is 3 && out_has "\"post_assign\":\"3\""'

nr_setup $PIN $PERM; touch "$work/nr/wedge"; WAIT=3 nr_run
chk "no boot change: rc 1 boot_not_confirmed" eval 'rc_is 1 && out_has boot_not_confirmed'
chk "no boot change: marker still written"    test -f "$work/nr/reboot/et8b1"

nr_setup $PIN $PERM; touch "$work/nr/factoryreset_fails"; nr_run
chk "FactoryReset fails: rc 1, no reboot, no marker" eval 'rc_is 1 && out_has factoryreset_failed && ! log_has "reboot -f" && [ ! -e "$work/nr/reboot/et8b1" ]'

nr_setup $PIN $PERM; RF_TASK='{"Id":"7","TaskState":"Running","PercentComplete":40}' nr_run
chk "busy job: rc 4, readable refusal"        eval 'rc_is 4 && err_has "refusing: BMC FW update job 7 running (Running 40%) on 172.17.8.101" && out_has "\"netreset\":\"refused\""'
chk "busy job: nothing reset, no marker"      eval '! log_has FactoryReset && ! log_has "reboot -f" && [ ! -e "$work/nr/reboot/et8b1" ]'

nr_setup $PIN $PERM; RF_TASK="{\"Id\":\"3\",\"TaskState\":\"Completed\",\"EndTime\":\"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)\"}" nr_run
chk "job ended < 60 s ago: rc 4 quiet_window" eval 'rc_is 4 && out_has quiet_window && ! log_has FactoryReset'

nr_setup $PIN $PERM; RF_TASK="{\"Id\":\"3\",\"TaskState\":\"Completed\",\"EndTime\":\"$(date -u -d @$(( $(date +%s) - 600 )) +%Y-%m-%dT%H:%M:%S+00:00)\"}" nr_run
chk "job ended 10 min ago: proceeds, clean"   eval 'rc_is 0 && out_has "\"netreset\":\"clean\""'

nr_setup $PIN $PERM
( flock -n 9 || exit 99; nr_run ) 9> "$work/nr/lock/fw-update-172.17.8.101.lock"
chk "lock held: rc 4, readable refusal"       eval 'rc_is 4 && err_has "refusing: another firmware update holds the lock for this BMC" && out_has "{\"netreset\":\"refused\",\"reason\":\"local_lock\"}"'
chk "lock held: nothing sent to the BMC"      eval '[ ! -s "$work/nr/log" ] && [ ! -e "$work/nr/manual/et8b1" ]'

nr_setup $PIN $PERM
nr_env "$work/bin" netreset 172.17.8.101 > /dev/null 2>&1; echo $? > "$work/nr/rc"
chk "no --port: usage rc 2, nothing sent"     eval 'rc_is 2 && [ ! -s "$work/nr/log" ]'
nr_env timeout 10 "$work/bin" netreset 172.17.8.101 --port > /dev/null 2>&1; echo $? > "$work/nr/rc"
chk "--port without a value: usage rc 2"      rc_is 2
nr_env "$work/bin" netreset 172.17.8.101 --port ../x > /dev/null 2>&1; echo $? > "$work/nr/rc"
chk "--port ../x: usage rc 2"                 rc_is 2
nr_env "$work/bin" netreset 'x/../../y' --port et8b1 > /dev/null 2>&1; echo $? > "$work/nr/rc"
chk "non-IPv4 address: usage rc 2, nothing sent" eval 'rc_is 2 && [ ! -s "$work/nr/log" ]'

# --- final-review fix: a FactoryReset busctl that never returns is bounded
#     and takes the factoryreset_failed path: no reboot, lock + claim freed. ---
nr_setup $PIN $PERM
t0=$(date +%s)
nr_env NR_RESET_HANG=600.331 NETRESET_RESET_TIMEOUT_S=2 timeout 40 "$work/bin" netreset 172.17.8.101 --port et8b1 > "$work/nr/out" 2> "$work/nr/err"
echo $? > "$work/nr/rc"; dt=$(( $(date +%s) - t0 ))
chk "hung FactoryReset: rc 1 factoryreset_failed within the bound (${dt}s)" eval 'rc_is 1 && out_has factoryreset_failed && [ "$dt" -le 10 ]'
chk "hung FactoryReset: NO reboot sent, no marker" eval '! log_has "reboot -f" && [ ! -e "$work/nr/reboot/et8b1" ]'
chk "hung FactoryReset: lock + claim released" eval 'lock_free && [ ! -e "$work/nr/manual/et8b1" ]'
chk "hung FactoryReset: no leftover hang"      eval '! pgrep -f "sleep 600\.331" >/dev/null'
pkill -9 -f 'sleep 600\.331' 2>/dev/null

# --- fix round 1: a `reboot -f` ssh that never returns (the BMC drops
#     without FIN/RST) and a post-reboot read that never answers must both be
#     bounded, or the boot-wait deadline never starts. Outer `timeout 40` so an
#     unbounded bin FAILS here instead of hanging the suite. ---
nr_setup $PIN $PERM
t0=$(date +%s)
nr_env NR_REBOOT_HANG=600.117 NETRESET_REBOOT_TIMEOUT_S=2 timeout 40 "$work/bin" netreset 172.17.8.101 --port et8b1 > "$work/nr/out" 2> "$work/nr/err"
echo $? > "$work/nr/rc"; dt=$(( $(date +%s) - t0 ))
chk "hung reboot ssh: bounded, reaches the post-state read, clean (${dt}s)" eval 'rc_is 0 && out_has "\"netreset\":\"clean\"" && [ "$dt" -le 15 ]'
chk "hung reboot ssh: no leftover hang"       eval '! pgrep -f "sleep 600\.117" >/dev/null'
pkill -9 -f 'sleep 600\.117' 2>/dev/null
nr_setup $PIN $PERM
t0=$(date +%s)
nr_env NR_POLL_HANG=600.223 NETRESET_READ_TIMEOUT_S=2 timeout 40 "$work/bin" netreset 172.17.8.101 --port et8b1 > "$work/nr/out" 2> "$work/nr/err"
echo $? > "$work/nr/rc"; dt=$(( $(date +%s) - t0 ))
chk "hung post-reboot read: bounded, falls through to the old IPv4 (${dt}s)" eval 'rc_is 0 && out_has "\"target\":\"172.17.8.101\"" && [ "$dt" -le 15 ]'
chk "hung post-reboot read: no leftover hang" eval '! pgrep -f "sleep 600\.223" >/dev/null'
pkill -9 -f 'sleep 600\.223' 2>/dev/null

# --- TERM to the PARENT while the first state read hangs: exits promptly
#     (143), lock free, claim removed, no survivor, nothing reset. ---
nr_setup $PIN $PERM
env "${NR_SEAMS[@]}" NETRESET_BOOT_WAIT_S=6 NR_HANG=31.417 "$work/bin" netreset 172.17.8.101 --port et8b1 > "$work/nr/out" 2> "$work/nr/err" &
bp=$!
for i in $(seq 1 100); do pgrep -f 'sleep 31\.417' >/dev/null 2>&1 && break; sleep 0.1; done
chk "TERM setup: hung in the state read, claim held" eval 'pgrep -f "sleep 31\.417" >/dev/null && [ -e "$work/nr/manual/et8b1" ]'
t0=$(date +%s); kill -TERM "$bp"
for i in $(seq 1 50); do kill -0 "$bp" 2>/dev/null || break; sleep 0.1; done
wait "$bp" 2>/dev/null; echo $? > "$work/nr/rc"; dt=$(( $(date +%s) - t0 ))
chk "TERM: parent exits 143 within 5 s (${dt}s)" eval 'rc_is 143 && [ "$dt" -le 5 ]'
chk "TERM: lock free, claim removed"          eval 'lock_free && [ ! -e "$work/nr/manual/et8b1" ]'
chk "TERM: no surviving hang"                 eval '! pgrep -f "sleep 31\.417" >/dev/null'
chk "TERM: nothing reset"                     eval '! log_has FactoryReset && ! log_has "reboot -f"'
pkill -9 -f 'sleep 31\.417' 2>/dev/null

# --- SIGKILL to the PARENT while the first state read hangs. The hang is
#     FINITE and then answers a real state, so a surviving CHILD would carry
#     on to FactoryReset + reboot after it: waiting past the hang and finding
#     neither in the log is what proves the child died with the parent. ---
nr_setup $PIN $PERM
env "${NR_SEAMS[@]}" NETRESET_BOOT_WAIT_S=6 NR_HANG=2.713 "$work/bin" netreset 172.17.8.101 --port et8b1 > "$work/nr/out" 2> "$work/nr/err" &
bp=$!
for i in $(seq 1 100); do pgrep -f 'sleep 2\.713' >/dev/null 2>&1 && break; sleep 0.1; done
kill -9 "$bp"; wait "$bp" 2>/dev/null
chk "KILL: lock free at once"                 lock_free
sleep 5
chk "KILL: the orphaned body never reset or rebooted the BMC" eval 'log_has "BOOT=" && ! log_has FactoryReset && ! log_has "reboot -f" && [ ! -e "$work/nr/reboot/et8b1" ]'


printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
