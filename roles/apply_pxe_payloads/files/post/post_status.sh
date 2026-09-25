# post_status.sh -- sourced by post.sh: live status on SOL + post-status.json.
#
# A VIEW, NEVER THE JOB. Every ps_* function returns the exit status it was
# called with, so `cmd; ps_done` leaves $? exactly as cmd set it, and nothing
# here can fail, slow or re-route an inventory run. No set -e/-u, ever.
#
# The state and rendering live in post_status.py (python3 is on the live ISO;
# without it every call is a no-op). This file owns the console side:
#   - the serial console is the last ttyS* in console= (ttyS0 TP, ttyS1
#     Leopard). None -> no timers, no reloads; the JSON is still written.
#   - serial_console_fixup.sh (verbatim from the mezz-flash station) re-probes
#     that port only if it came up uart:unknown (et9b1: 9 of 16 boots), then
#     proves it transmits or reverts it. It runs here, late, never at early
#     boot. The ISO has no setserial, so post.tgz ships the bang's, and the
#     hook dir goes first on PATH.
#   - agetty --reload after every write, a 20s redraw timer (the banner's
#     clock line makes every reload repaint), and the station's stall
#     watchdog every 30s on that tty.
#   - an EXIT trap: a run that ends without ps_finish shows FAILED, unless it
#     is REBOOTING / POWER-OFF / WAITING (left alone), or the system itself is
#     shutting down (a flash's queued reboot) -- then REBOOTING.

_ps_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_ps_python="${POST_STATUS_PYTHON:-python3}"
_ps_tty=""
_ps_on=0
command -v "$_ps_python" >/dev/null 2>&1 && [ -f "$_ps_dir/post_status.py" ] && _ps_on=1

function _ps()
{
    [ "$_ps_on" = 1 ] || return 0
    "$_ps_python" "$_ps_dir/post_status.py" "$@" >/dev/null 2>&1
    [ -n "$_ps_tty" ] && agetty --reload >/dev/null 2>&1
    return 0
}

function ps_init()          # ps_init <action>
{
    local rc=$? bundle
    _ps_tty=$(tr ' ' '\n' < "${POST_CONSOLE_ACTIVE:-/sys/class/tty/console/active}" 2>/dev/null \
                | grep '^ttyS[0-9]' | tail -1)
    if [ -n "$_ps_tty" ]; then
        if [ -f "$_ps_dir/serial_console_fixup.sh" ]; then
            PATH="$_ps_dir:$PATH" bash "$_ps_dir/serial_console_fixup.sh" 2>&1 \
                | sed 's/^/post_status: /'
        fi
        systemd-run --quiet --unit=flax-post-banner --on-active=20 --on-unit-active=20 \
            agetty --reload >/dev/null 2>&1
        [ -f "$_ps_dir/serial_watchdog.sh" ] && \
            systemd-run --quiet --unit=flax-post-watchdog --on-active=30 --on-unit-active=30 \
                --setenv=MEZZ_WD_STATE=/run/flax/post-wd \
                /bin/bash "$_ps_dir/serial_watchdog.sh" "$_ps_tty" >/dev/null 2>&1
    fi
    bundle=$(head -1 "$_ps_dir/BUILD" 2>/dev/null)
    _ps init "$1" "$(hostname 2>/dev/null)" "${bundle:-unknown}"
    # The system state rides along: a flash that queues `shutdown -r now` and
    # returns lets post.sh start the next stage before systemd's SIGTERM
    # lands -- "stopping" says that is a reboot, not a crash.
    trap '_ps exit $? "$(systemctl is-system-running 2>/dev/null)"' EXIT
    return $rc
}

function ps_begin()  { local rc=$?; _ps begin "$@";  return $rc; }   # <stage> [note]
function ps_note()   { local rc=$?; _ps note "$@";   return $rc; }   # <text>
function ps_done()   { local rc=$?; _ps done "$@";   return $rc; }   # [detail]
function ps_fail()   { local rc=$?; _ps fail "$@";   return $rc; }   # [detail]
function ps_skip()   { local rc=$?; _ps skip "$@";   return $rc; }   # <stage> <detail>
function ps_state()  { local rc=$?; _ps state "$@";  return $rc; }   # <STATE> <text>
function ps_set()    { local rc=$?; _ps set "$@";    return $rc; }   # <key> <value>
function ps_finish() { local rc=$?; _ps finish "$@"; return $rc; }   # <STATE> <text>
