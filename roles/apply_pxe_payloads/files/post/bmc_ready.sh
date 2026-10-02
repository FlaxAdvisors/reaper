# bmc_ready.sh -- sourced by post.sh and update_mellanox.sh: never talk to a
# BMC that is not there.
#
# The BMC can go away under a run. update_mellanox.sh cold-resets it after it
# turns a NIC's UEFI option on, and post.sh used to walk straight into the
# inventory: `ipmitool fru` hung 50 s, every later read came back empty, and
# the final `ipmitool chassis power off` was refused ("Command not supported
# in present state"), so the blade stayed on with a green banner (eindhoven
# 2026-10-02; 32 runs since 2026-09-25, when unlocked NICs started arriving
# with UEFI off).
#
# bmc_gate is the one test every in-band IPMI section goes through:
#   - ready = `ipmitool mc info` AND `ipmitool chassis status` both answer,
#     twice in a row. mc info alone is not enough: ipmid answers it while the
#     BMC's power/state services are still starting.
#   - a BMC that was asked to reset (bmc_reset_cold leaves a marker) is NOT
#     ready while it is still answering from its old boot: the gate waits for
#     it to go down first, or for BMC_DOWN_GRACE to pass without that.
#   - a BMC that stays silent gets the host's IPMI driver restarted (the
#     service that provides /dev/ipmi0), every BMC_DRIVER_RESTART_AFTER.
#   - it gives up after BMC_WAIT_MAX and says so; the caller then skips its
#     IPMI commands instead of hanging on each one.
# The fast path is two quick commands, so gating a section costs nothing on a
# healthy BMC.
#
# No set -e/-u, like post.sh. ps_note/ps_set come from post_status.sh when the
# caller sourced it; without it the progress still lands in the journal.

BMC_WAIT_MAX=${BMC_WAIT_MAX:-420}
BMC_PROBE_EVERY=${BMC_PROBE_EVERY:-5}
BMC_PROBE_TIMEOUT=${BMC_PROBE_TIMEOUT:-15}
BMC_DOWN_GRACE=${BMC_DOWN_GRACE:-90}
BMC_DRIVER_RESTART_AFTER=${BMC_DRIVER_RESTART_AFTER:-60}
BMC_RESET_MARK=${BMC_RESET_MARK:-/run/flax/bmc-reset-requested}
# Boot clock, not wall time: chronyd steps the wall clock early in post.sh.
BMC_NOW_CMD=${BMC_NOW_CMD:-cut -d. -f1 /proc/uptime}

bmc_ok=1            # the last gate's verdict, for callers that want it later

function _bmc_now()  { $BMC_NOW_CMD 2>/dev/null; }

function _bmc_say()         # banner (when post_status.sh is loaded) + journal
{
    echo "bmc_ready: $1"
    declare -F ps_note >/dev/null && ps_note "$1"
    declare -F ps_set  >/dev/null && ps_set bmc "${2-$1}"
    return 0
}

function bmc_probe()
{
    timeout "$BMC_PROBE_TIMEOUT" ipmitool mc info >/dev/null 2>&1 || return 1
    timeout "$BMC_PROBE_TIMEOUT" ipmitool chassis status >/dev/null 2>&1
}

# The host side of the KCS link: whichever service loaded the driver, else the
# modules themselves. Harmless while the BMC is still down -- it is tried again.
function bmc_driver_restart()
{
    echo "bmc_ready: restarting the host IPMI driver"
    systemctl restart openipmi 2>/dev/null && return 0
    systemctl restart ipmi 2>/dev/null && return 0
    modprobe -r ipmi_devintf ipmi_si 2>/dev/null
    modprobe ipmi_si 2>/dev/null
    modprobe ipmi_devintf 2>/dev/null
}

# Ask the BMC to cold-reset and remember that we did, so the next gate does
# not mistake its last few seconds of answering for "ready".
function bmc_reset_cold()
{
    ipmitool mc reset cold
    local rc=$?
    if [ $rc -eq 0 ]; then
        mkdir -p "$(dirname "$BMC_RESET_MARK")" 2>/dev/null
        _bmc_now > "$BMC_RESET_MARK"
    fi
    return $rc
}

# bmc_gate <what for>  -> 0 the BMC is ready, 1 it is not (bmc_ok mirrors it)
function bmc_gate()
{
    local what="${1:-ipmi}" start now waited reset_at good=0 down_seen=0
    local silent_since="" last_restart=""
    # No in-band IPMI on this system at all (post.sh's ipmigood=0): nothing
    # to wait for. Unset (update_mellanox.sh) counts as "has IPMI".
    if [ "${ipmigood:-1}" = 0 ]; then
        bmc_ok=0
        return 1
    fi
    reset_at=$(cat "$BMC_RESET_MARK" 2>/dev/null)
    case "$reset_at" in ''|*[!0-9]*) reset_at="" ;; esac

    if [ -z "$reset_at" ] && bmc_probe; then
        bmc_ok=1
        return 0
    fi

    start=$(_bmc_now)
    while :; do
        now=$(_bmc_now)
        waited=$(( now - start ))
        if bmc_probe; then
            silent_since=""
            if [ -n "$reset_at" ] && [ $down_seen -eq 0 ] \
                    && [ $(( now - reset_at )) -lt "$BMC_DOWN_GRACE" ]; then
                good=0
                _bmc_say "BMC reset requested -- waiting for it to restart ($(( now - reset_at ))s)" \
                         "restarting (reset requested)"
            else
                good=$(( good + 1 ))
                if [ $good -ge 2 ]; then
                    rm -f "$BMC_RESET_MARK"
                    [ $waited -gt 0 ] && echo "bmc_ready: BMC ready for $what after ${waited}s"
                    declare -F ps_set >/dev/null && ps_set bmc ""
                    bmc_ok=1
                    return 0
                fi
            fi
        else
            good=0
            down_seen=1
            [ -z "$silent_since" ] && silent_since=$now
            _bmc_say "BMC not answering -- waiting for it before $what (${waited}s of ${BMC_WAIT_MAX}s)" \
                     "not answering (${waited}s)"
            if [ $(( now - silent_since )) -ge "$BMC_DRIVER_RESTART_AFTER" ] \
                    && [ $(( now - ${last_restart:-$silent_since} )) -ge "$BMC_DRIVER_RESTART_AFTER" ]; then
                bmc_driver_restart
                last_restart=$now
            fi
        fi
        if [ $waited -ge "$BMC_WAIT_MAX" ]; then
            _bmc_say "BMC still not answering after ${waited}s -- skipping $what" "UNREACHABLE"
            bmc_ok=0
            return 1
        fi
        sleep "$BMC_PROBE_EVERY"
    done
}
