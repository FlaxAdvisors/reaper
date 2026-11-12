#!/bin/bash
# spichip -- name the SPI NOR flash chips on this host from their SFDP tables.
#
# The kernel's part name and JEDEC id are the SAME for MX25L25635F and the
# 4-byte-only MX25L25735F (both "mx25l25635e", c22019). Only the chip's SFDP
# table tells them apart: bits 2:1 of the third byte of the basic table's
# first word (e5 20 f3 ff vs e5 20 f5 ff). A Quanta Tioga Pass does not boot
# from the 4-byte-only part; a Wiwynn does (2026-10-02).
#
#   spichip               every /sys/bus/spi/devices/*/spi-nor on this host
#   spichip -v            the same, plus the raw SFDP as hex
#   spichip <file>...     decode raw SFDP dumps (a copy of a sysfs sfdp file)
#
# Runs on a booted host (the BIOS chip is the PCH's SPI NOR). With the host
# off, read the chip from the BMC instead: fb-bios-update --regs.
# Exit status: 0 if at least one chip was decoded, 1 otherwise.

verbose=0
[ "$1" = "-v" ] && { verbose=1; shift; }
case "$1" in
    -h|--help) sed -n '2,16s/^# \{0,1\}//p' "$0"; exit 0 ;;
esac

# addr_mode <sfdp hex>: 3-byte-only / 3-byte+4-byte / 4-byte-only / unknown
addr_mode() {
    local sfdp=$1 p b
    [ "${sfdp:0:8}" = "53464450" ] || { echo unknown; return; }
    # first parameter header is at byte 8; its 3-byte table pointer at 12
    p=$(( 0x${sfdp:28:2}${sfdp:26:2}${sfdp:24:2} ))
    b=${sfdp:$(( (p + 2) * 2 )):2}
    case "$(( (0x${b:-0} >> 1) & 3 ))" in
        0) echo 3-byte-only ;;
        1) echo 3-byte+4-byte ;;
        2) echo 4-byte-only ;;
        *) echo unknown ;;
    esac
}

# part <jedec_id> <addr_mode>: the marking, where the id alone is ambiguous
part() {
    case "$1/$2" in
        c22019/3-byte+4-byte) echo MX25L25635F ;;
        c22019/4-byte-only)   echo MX25L25735F ;;
        *)                    echo - ;;
    esac
}

found=0
report() {   # report <label> <partname> <manufacturer> <jedec_id> <sfdp file>
    local sfdp mode
    sfdp=$(od -An -v -tx1 "$5" 2>/dev/null | tr -d ' \n')
    mode=$(addr_mode "$sfdp")
    [ "$mode" != unknown ] && found=1
    echo "dev=$1 partname=$2 manufacturer=$3 jedec_id=$4 addr_mode=$mode part=$(part "$4" "$mode")"
    [ "$verbose" = 1 ] && echo "sfdp=$sfdp"
}

if [ $# -gt 0 ]; then
    for f in "$@"; do
        [ -r "$f" ] || { echo "spichip: cannot read $f" >&2; continue; }
        # a bare SFDP dump carries no JEDEC id, so no part name: read addr_mode
        report "$f" - - - "$f"
    done
else
    for d in /sys/bus/spi/devices/*/spi-nor; do
        [ -d "$d" ] || continue
        report "$(basename "$(dirname "$d")")" "$(cat "$d/partname" 2>/dev/null)" \
               "$(cat "$d/manufacturer" 2>/dev/null)" "$(cat "$d/jedec_id" 2>/dev/null)" "$d/sfdp"
    done
    [ "$found" = 1 ] || echo "spichip: no SPI NOR with a readable SFDP table under /sys/bus/spi/devices" >&2
fi
[ "$found" = 1 ]
