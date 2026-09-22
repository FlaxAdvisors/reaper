#!/bin/bash
# serial_console_fixup.sh -- make sure the SOL console has a login prompt.
#
# On et9b1 (2026-09-22) the kernel left the SOL UART -- the BMC's virtual
# UART -- as `uart:unknown` on 9 of 16 boots, so systemd never started a serial
# getty and SOL showed nothing: no prompt, no station banner. Operators have no
# other serial port and no debug cable, so the fix has to be on this port.
#
# Measured on the live board: a late re-probe WITH the IRQ brings it back clean
# (`setserial ... autoconfig auto_irq`, IRQ firing, full banner on SOL).
# Forcing the type without autoconfig left TX dead (tx:0), and polled mode
# (irq 0) dropped about half the characters -- do not "simplify" to either.
#
# The port is whatever the LAST console= names (/sys/class/tty/console/active),
# so Leopard (ttyS1) and Tioga Pass need no per-board knowledge here.

info="${MEZZ_SERIAL_INFO:-/proc/tty/driver/serial}"
active="${MEZZ_CONSOLE_ACTIVE:-/sys/class/tty/console/active}"

tty=$(tr ' ' '\n' < "$active" 2>/dev/null | grep '^ttyS[0-9]' | tail -1)
[ -n "$tty" ] || exit 0
n=${tty#ttyS}
# The legacy ISA resources the 8250 driver itself assumes for these ports.
case "$n" in
    0) port=0x3f8; irq=4 ;;
    1) port=0x2f8; irq=3 ;;
    2) port=0x3e8; irq=4 ;;
    3) port=0x2e8; irq=3 ;;
    *) exit 0 ;;
esac

if grep "^$n:" "$info" 2>/dev/null | grep -q "uart:unknown"; then
    echo "$tty: SOL UART not detected at boot; re-probing ($port irq $irq)"
    setserial "/dev/$tty" port "$port" irq "$irq" autoconfig auto_irq
    grep "^$n:" "$info" 2>/dev/null
    reprobed=1
fi
# The getty generator skipped the port if it was dead when it ran, so start
# the prompt ourselves. Idempotent when it is already running.
systemctl --no-block start "serial-getty@$tty.service"

# Prove it actually transmits before leaving it alive. A dead port is harmless
# -- writes to it fail fast -- but a port that is alive and NOT draining costs
# PID1 ~30s per console line (et9b1, 2026-09-22), and that is what turns a
# boot or a shutdown into twenty minutes. If the re-probe did not restore TX,
# put the port back to `uart none` and stop the prompt: a console nobody can
# read beats a console that blocks everyone.
function txnow()
{
    if [ -n "$MEZZ_TX_PROBE_INFO" ]; then
        "$MEZZ_TX_PROBE_INFO"
    else
        cat "$info" 2>/dev/null
    fi | grep "^$n:" | sed -nE 's/.* tx:([0-9]+).*/\1/p'
}

# Only for a port we woke: one that was already up is the kernel's business.
[ "${reprobed:-0}" = "1" ] || exit 0

before=$(txnow)
# Make the waiting prompt repaint, which is the traffic we then measure.
agetty --reload >/dev/null 2>&1 || true
sleep "${MEZZ_TX_SETTLE:-2}"
after=$(txnow)
if [ -n "$before" ] && [ "$before" = "$after" ]; then
    echo "$tty: re-probed but still not transmitting (tx stuck at $before);" \
         "reverting so console writes fail fast instead of blocking"
    systemctl stop "serial-getty@$tty.service" >/dev/null 2>&1 || true
    setserial "/dev/$tty" uart none 2>/dev/null || true
    exit 0
fi
echo "$tty: transmitting again (tx $before -> $after)"
exit 0
