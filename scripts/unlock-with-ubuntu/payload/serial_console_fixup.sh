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
fi
# The getty generator skipped the port if it was dead when it ran, so start
# the prompt ourselves. Idempotent when it is already running.
systemctl --no-block start "serial-getty@$tty.service"
exit 0
