#!/bin/bash
# serial_watchdog.sh -- un-stick the SOL console when its UART stops sending.
#
# et9b1, 2026-09-22: the SOL UART stopped transmitting mid-run. tx sat at
# 64925 across 25s while the banner rewrote every 20s, rx was 0 (so operator
# keystrokes were not arriving either), and the triage SOL showed nothing.
# `setserial ... autoconfig` cannot help there -- the getty holds the port, so
# it is refused with "Device or resource busy" -- but restarting the getty
# re-opens the port, re-arms its TX interrupt and moved tx again at once
# (64925 -> 66422, full banner on SOL).
#
# So: sample tx; if it has not moved since the last tick, the port is stuck
# (the banner timer writes every 20s, this runs every 30s, so a healthy port
# always advances). Restart the login prompt, at most once per cooldown.
#
# RUNTIME ONLY. If the port stalls during boot, PID1 is itself stuck writing
# to /dev/console and no timer gets to run; a BMC cold reset is the cure for
# that one (it resets the BMC's end of the virtual UART).
info="${MEZZ_SERIAL_INFO:-/proc/tty/driver/serial}"
active="${MEZZ_CONSOLE_ACTIVE:-/sys/class/tty/console/active}"
state="${MEZZ_WD_STATE:-/run/flax/mezz-flash-wd}"
now="${MEZZ_WD_NOW:-$(date +%s)}"
cooldown="${MEZZ_WD_COOLDOWN:-300}"

# An explicit tty wins (post passes the one it found); otherwise the console.
tty="${1:-}"
[ -n "$tty" ] || tty=$(tr ' ' '\n' < "$active" 2>/dev/null | grep '^ttyS[0-9]' | tail -1)
[ -n "$tty" ] || exit 0
n=${tty#ttyS}
tx=$(grep "^$n:" "$info" 2>/dev/null | sed -nE 's/.* tx:([0-9]+).*/\1/p')
[ -n "$tx" ] || exit 0

prevtx=""
lastfix=0
[ -f "$state" ] && read -r prevtx lastfix < "$state"
lastfix="${lastfix:-0}"
mkdir -p "$(dirname "$state")" 2>/dev/null
printf '%s %s\n' "$tx" "$lastfix" > "$state" 2>/dev/null

# First sample, or still moving: nothing to do.
[ -n "$prevtx" ] || exit 0
[ "$tx" = "$prevtx" ] || exit 0

# Never pull the rug out from under an operator: restarting the getty kills a
# login session on that tty. It is also the one case where a frozen counter is
# expected -- an idle shell writes nothing. Retried on the next tick, so it
# heals by itself once they log out.
if who 2>/dev/null | awk '{print $2}' | grep -qx "$tty" \
   || loginctl list-sessions --no-legend 2>/dev/null | grep -qw "$tty"; then
    echo "$tty: TX frozen at $tx, but a login session is on it -- not restarting"
    exit 0
fi

# A port with no prompt on it is the undetected-UART case, not this one.
[ "$(systemctl is-active "serial-getty@$tty.service" 2>/dev/null)" = "active" ] || exit 0

# Never loop: one restart per cooldown, however long it stays stuck.
[ $((now - lastfix)) -ge "$cooldown" ] || exit 0

echo "$tty: TX frozen at $tx -- restarting serial-getty@$tty"
systemctl restart "serial-getty@$tty.service"
printf '%s %s\n' "$tx" "$now" > "$state" 2>/dev/null
exit 0
