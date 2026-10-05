#!/bin/bash
# fb-bios-update -- the upstream Facebook tiogapass bios-update
# (meta-facebook/meta-tiogapass/recipes-phosphor/flash/phosphor-software-manager/
# bios-update), ported to the flax-onetree BMC with the fewest possible changes.
# Every deviation from upstream is marked "ONETREE:".
#
# Usage:
#   fb-bios-update [--no-poweron] <image.bin>
#                                full-chip flash, FB sequence:
#                                host off -> ME recovery -> mux to BMC -> flashcp
#                                -> mux to PCH -> ME cold reset -> host on
#   fb-bios-update <dir>         as above with <dir>/image-bios: the form the BMC's
#                                update service uses when this file is installed
#                                as /usr/sbin/bios-update
#   fb-bios-update --read <out>  same sequence, but reads the whole chip (twice,
#                                compared) instead of writing it; the host is
#                                left in the power state it was found in
#   fb-bios-update --regs        same sequence, but only reads the chip's status,
#                                configuration and security registers (read-only)
#   fb-bios-update --me-info     only query the ME (Get Device ID + self test)
#   fb-bios-update --inspect     READ-ONLY: is the ME answering, and if it is
#                                not, which chip part is fitted (SFDP), is it
#                                blank, and what structure does it hold. Prints
#                                "inspect: key=value" lines. No power action, no
#                                ME recovery, no ME reset, no chip write. The SPI
#                                bus is NOT taken if the ME answers.
#
# The person running this does not need to know what the ME is doing; the
# script finds out and says what it did:
#   ME running      -> put into recovery; if it will not go, abort before any
#                      write (host left off, chip untouched)
#   ME in recovery  -> flash
#   ME not answering for ME_PROBE_S (30 s) -> not running, flash without
#                      recovery
# After the flash the ME is always sent the cold reset and given
# ME_RESET_WAIT_S (60 s) to report normal; the last line is a summary.
#
# ONETREE: upstream takes a directory and flashes $1/bios.bin; this takes the
# image file itself.

set -e

# ONETREE: upstream uses "/usr/sbin/power-util mb"; onetree has obmcutil and
# reports state on D-Bus.
power_off() { obmcutil poweroff; }
power_on()  { obmcutil poweron; }
power_status() {
    busctl get-property xyz.openbmc_project.State.Host \
        /xyz/openbmc_project/state/host0 \
        xyz.openbmc_project.State.Host CurrentHostState 2>/dev/null \
        | awk '{print $2}' | tr -d '"' | sed 's/.*\.//' | tr 'A-Z' 'a-z'
}

# ONETREE: upstream hardcodes GPIO=389. Same line (N5, BIOS_SPI_BMC_CTRL),
# computed from the gpiochip base so it survives a base change.
GPIO_BASE=$(sort -n /sys/class/gpio/gpiochip*/base 2>/dev/null | head -n 1)
GPIO=$(( GPIO_BASE + 109 ))

# ONETREE: upstream sends ME commands through ipmbbridged on D-Bus
# (xyz.openbmc_project.Ipmi.Channel.Ipmb). onetree does not run ipmbbridged,
# so the same requests go out as raw IPMB frames on /dev/ipmb-4 (ME at 0x2c).
# Same commands as upstream:
#   ME_CMD_RECOVER="1 0x2e 0 0xdf 4 0x57 0x01 0x00 0x01"   (Force ME Recovery)
#   ME_CMD_RESET="1 6 0 0x2 0"                              (Cold Reset)
IPMB_DEV=/dev/ipmb-4
ME_SA=0x2c
BMC_SA=0x20
# (sequence number: see next_seq below)

SPI_DEV="1e630000.spi"
# ONETREE: upstream driver name is "aspeed-smc"; this kernel's is spi-aspeed-smc.
SPI_PATH="/sys/bus/platform/drivers/spi-aspeed-smc"

# ONETREE: everything from here to set_gpio_to_bmc is new. Upstream fires its
# two ME commands blind (busctl succeeds whatever the ME answers). Here the ME
# may be running, already in recovery, or not running at all (no I2C ack), and
# which one it is is only known by asking, so the script asks and branches.

# ipmb_req <netfn> <cmd> [data...] -> 0 = answered with CC 00, 1 = answered
# with another CC, 2 = no ack / no (matching) response. Sets IPMB_RESP (hex
# bytes: len rqSA netfn cs1 rsSA seq cmd CC data... cs) and IPMB_CC.
IPMB_RESP=""
IPMB_CC=""
IPMB_REPLY_S=${IPMB_REPLY_S:-5}         # how long to wait for THIS request's reply
# The sequence number lives in a file: me_state/me_wait run in $( ) subshells,
# and a shell variable bumped there never advances in the caller -- every poll
# would reuse one number and a stale reply could match a later poll.
IPMB_SEQ_FILE=/tmp/.fb-bios-update.seq
next_seq() {
    local s
    s=$(cat "${IPMB_SEQ_FILE}" 2>/dev/null) || s=0
    s=$(( ( ${s:-0} + 1 ) & 0x3f )); [ "${s}" -eq 0 ] && s=1
    echo "${s}" > "${IPMB_SEQ_FILE}"
    echo "${s}"
}
# One queued IPMB message as hex, or nothing within $1 seconds. The kernel's
# ipmb-dev-int QUEUES every message addressed to the BMC; a reply that arrives
# after its requester gave up stays queued and would be read as the reply to
# the next request.
ipmb_read1() {
    echo $(timeout "$1" dd if="${IPMB_DEV}" bs=64 count=1 2>/dev/null \
        | hexdump -v -e '64/1 "%02x "')
}
ipmb_req() {
    local netfn=$(( $1 )) cmd=$(( $2 )) b sum frame wrc m i deadline
    shift 2
    local seq; seq=$(next_seq)
    local hdr=( $(( ME_SA )) $(( netfn << 2 )) )
    local cs1=$(( (0x100 - ((hdr[0] + hdr[1]) & 0xff)) & 0xff ))
    local body=( $(( BMC_SA )) $(( seq << 2 )) "${cmd}" )
    for b in "$@"; do body+=( $(( b )) ); done
    sum=0; for b in "${body[@]}"; do sum=$(( sum + b )); done
    local cs2=$(( (0x100 - (sum & 0xff)) & 0xff ))
    local all=( "${hdr[@]}" "${cs1}" "${body[@]}" "${cs2}" )
    frame=$(printf '\\%03o' "${#all[@]}" "${all[@]}")
    IPMB_RESP=""
    IPMB_CC=""
    # Drain anything already queued (late replies to earlier requests).
    for i in $(seq 1 20); do
        m=$(ipmb_read1 1)
        [ -z "${m}" ] && break
        echo "  (discarded stale IPMB message: ${m})" >&2
    done
    wrc=0
    printf "${frame}" > "${IPMB_DEV}" 2>/dev/null || wrc=$?
    [ "${wrc}" -ne 0 ] && return 2          # no I2C ack from the ME
    # Read until the reply to THIS request arrives -- from the ME, response
    # netfn, same cmd, same seq -- discarding anything else, or time runs out.
    deadline=$(( $(date +%s) + IPMB_REPLY_S ))
    while [ "$(date +%s)" -lt "${deadline}" ]; do
        m=$(ipmb_read1 2)
        [ -z "${m}" ] && continue
        set -- ${m}
        if [ $# -ge 8 ] && [ "$5" = "$(printf %02x $(( ME_SA )))" ] \
           && [ "$7" = "$(printf %02x "${cmd}")" ] \
           && [ "$(( 0x$6 >> 2 ))" -eq "${seq}" ] \
           && [ "$(( 0x$3 >> 2 ))" -eq $(( netfn + 1 )) ]; then
            IPMB_RESP="${m}"
            IPMB_CC=$8
            [ "${IPMB_CC}" = "00" ] && return 0
            return 1
        fi
        echo "  (discarded non-matching IPMB message: ${m})" >&2
    done
    return 2                                # no reply to THIS request
}

# me_state -> echoes one of: normal (self test 55 00), recovery (81 xx),
# silent (no ack / no response), other:<bytes> (answered, anything else).
me_state() {
    local rc=0
    ipmb_req 0x06 0x04 || rc=$?
    case "${rc}" in
        2) echo silent; return 0 ;;
        1) echo "other:cc=${IPMB_CC}"; return 0 ;;
    esac
    set -- ${IPMB_RESP}
    case "$9" in
        55) echo normal ;;
        81) echo recovery ;;
        *)  echo "other:$9 ${10}" ;;
    esac
}

# me_wait <seconds> <state...> -> polls every 3 s until me_state is one of the
# given states or time runs out; echoes the last state seen.
me_wait() {
    local limit=$1 t=0 st
    shift
    while :; do
        st=$(me_state)
        for want in "$@"; do [ "${st}" = "${want}" ] && { echo "${st}"; return 0; }; done
        [ "${t}" -ge "${limit}" ] && { echo "${st}"; return 0; }
        sleep 3; t=$(( t + 3 ))
    done
}

ME_PROBE_S=${ME_PROBE_S:-30}            # how long a silent ME gets to answer
ME_RECOVERY_WAIT_S=${ME_RECOVERY_WAIT_S:-60}
ME_RESET_WAIT_S=${ME_RESET_WAIT_S:-60}

me_info() {
    local rc=0
    echo "ME Get Device ID:"
    ipmb_req 0x06 0x01 || rc=$?
    case "${rc}" in
        0) echo "  ${IPMB_RESP}" ;;
        1) echo "  answered, CC ${IPMB_CC}: ${IPMB_RESP}" ;;
        2) echo "  no response (no I2C ack or no matching reply)" ;;
    esac
    echo "ME state (self test): $(me_state)"
}

set_gpio_to_bmc()
{
    echo "switch bios GPIO to bmc"
    if [ ! -d /sys/class/gpio/gpio$GPIO ]; then
        cd /sys/class/gpio
        echo $GPIO > "export"
        cd gpio$GPIO
    else
        cd /sys/class/gpio/gpio$GPIO
    fi
    direc=$(cat direction)
    if [ "$direc" == "in" ]; then
        echo "out" > direction
    fi
    data=$(cat value)
    if [ "$data" == "0" ]; then
        echo 1 > value
    fi
    return 0
}

set_gpio_to_pch()
{
    echo "switch bios GPIO to pch"
    if [ ! -d /sys/class/gpio/gpio$GPIO ]; then
        cd /sys/class/gpio
        echo $GPIO > "export"
        cd gpio$GPIO
    else
        cd /sys/class/gpio/gpio$GPIO
    fi
    direc=$(cat direction)
    if [ "$direc" == "in" ]; then
        echo "out" > direction
    fi
    data=$(cat value)
    if [ "$data" == "1" ]; then
        echo 0 > value
    fi
    echo "in" > direction
    echo $GPIO > /sys/class/gpio/unexport
    return 0
}

# ONETREE: the systemd hardware watchdog on this BMC (RuntimeWatchdogUSec,
# 2 min, pretimeout=panic) has reset the BMC in the middle of a ~4 min BIOS
# flashcp. Upstream has no equivalent; disarm only around the write.
WDT_MGR="org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager"
wdt_saved=""
wdt_disarm() {
    wdt_saved=$(busctl get-property ${WDT_MGR} RuntimeWatchdogUSec 2>/dev/null | awk '{print $2}')
    case "${wdt_saved}" in ''|0) wdt_saved=120000000 ;; esac
    busctl set-property ${WDT_MGR} RuntimeWatchdogUSec t 0 || true
}
wdt_rearm() {
    [ -n "${wdt_saved}" ] && busctl set-property ${WDT_MGR} RuntimeWatchdogUSec t "${wdt_saved}" || true
    wdt_saved=""
}

# ONETREE: --regs. Reads the BIOS chip's non-volatile control registers, which
# live outside the 32 MiB array and which flashcp/dd/cmp can neither see nor
# change. Uses the AST2500 SPI1 controller's USER mode (the same mechanism the
# kernel driver uses for raw commands) via devmem. READ-ONLY commands only:
#   9F RDID (must be c2 20 19, else nothing below is trusted)
#   05 RDSR  status: WIP WEL BP0..BP3 QE SRWD
#   15 RDCR  configuration (Macronix): ODS0..2 TB(OTP) - 4BYTE DC0 DC1
#   2B RDSCUR security: secured-OTP indicator, LDSO lock, P_FAIL, E_FAIL ...
# The controller's config and CE0-control registers are saved first and always
# restored. Addresses from the device tree: regs 0x1e630000, CE0 window
# 0x30000000 (segment register 0x64600000).
SPI1_REGS=0x1e630000
SPI1_WIN=0x30000000
spi_regs() {
    local cfg ctl user jedec sr cr scur
    cfg=$(devmem ${SPI1_REGS} 32)
    ctl=$(devmem $(( SPI1_REGS + 0x10 )) 32)
    echo "SPI1 controller: config ${cfg}, CE0 control ${ctl}"
    # user mode (bits 1:0 = 3), single-bit I/O (clear bits 29:28), keep clocks
    user=$(( (ctl & ~0x30000003) | 0x3 ))
    devmem ${SPI1_REGS} 32 $(( cfg | 0x10000 ))     # CE0 write-enable for the opcode byte
    spi_cmd() {         # spi_cmd "<opcode> [addr/dummy bytes...]" <nbytes> -> hex bytes
        local i b out=""
        devmem $(( SPI1_REGS + 0x10 )) 32 $(( user | 0x4 ))   # CS high
        devmem $(( SPI1_REGS + 0x10 )) 32 ${user}            # CS low
        for b in $1; do devmem ${SPI1_WIN} 8 $b; done
        for i in $(seq 1 $2); do out="${out} $(printf %02x $(( $(devmem ${SPI1_WIN} 8) )))"; done
        devmem $(( SPI1_REGS + 0x10 )) 32 $(( user | 0x4 ))   # CS high
        echo ${out}
    }
    jedec=$(spi_cmd 0x9f 3)
    sr=$(spi_cmd 0x05 1)
    cr=$(spi_cmd 0x15 1)
    scur=$(spi_cmd 0x2b 1)
    # More READ-ONLY identity/protection state (opcodes a part does not
    # implement just read back as floating bytes). None of these writes.
    local res rems lr fbr ear spblk spb0 dpb0 sfdp
    res=$(spi_cmd "0xab 0 0 0" 1)          # RES   electronic signature
    rems=$(spi_cmd "0x90 0 0 0" 2)         # REMS  manufacturer + device id
    lr=$(spi_cmd 0x2d 2)                   # RDLR  lock register
    fbr=$(spi_cmd 0x16 4)                  # RDFBR fast-boot register
    ear=$(spi_cmd 0xc8 1)                  # RDEAR extended address register
    spblk=$(spi_cmd 0xa7 1)                # RDSPBLK SPB lock bit
    spb0=$(spi_cmd "0xe2 0 0 0 0" 1)       # RDSPB @0 (4-byte address)
    dpb0=$(spi_cmd "0xe0 0 0 0 0" 1)       # RDDPB @0 (4-byte address)
    sfdp=$(spi_cmd "0x5a 0 0 0 0" 128)     # RDSFDP: 3 address bytes + 1 dummy, 128 bytes
    devmem $(( SPI1_REGS + 0x10 )) 32 ${ctl}        # restore, always
    devmem ${SPI1_REGS} 32 ${cfg}
    echo "SPI1 controller restored: config $(devmem ${SPI1_REGS} 32), CE0 control $(devmem $(( SPI1_REGS + 0x10 )) 32)"
    echo "RDID   (9F): ${jedec}"
    if [ "${jedec}" != "c2 20 19" ]; then
        echo "JEDEC ID is not c2 20 19 -- the user-mode read did not work; ignore the values below"
        echo "RDSR (05): ${sr}  RDCR (15): ${cr}  RDSCUR (2B): ${scur}"
        return 1
    fi
    local s=$(( 0x${sr} )) c=$(( 0x${cr} )) k=$(( 0x${scur} ))
    echo "RDSR   (05): ${sr}  WIP=$(( s & 1 )) WEL=$(( s>>1 & 1 )) BP0-3=$(( s>>2 & 0xf )) QE=$(( s>>6 & 1 )) SRWD=$(( s>>7 & 1 ))"
    echo "RDCR   (15): ${cr}  ODS=$(( c & 7 )) TB=$(( c>>3 & 1 )) 4BYTE=$(( c>>5 & 1 )) DC=$(( c>>6 & 3 ))"
    echo "RDSCUR (2B): ${scur}  SOI=$(( k & 1 )) LDSO=$(( k>>1 & 1 )) bit2-4=$(( k>>2 & 7 )) P_FAIL=$(( k>>5 & 1 )) E_FAIL=$(( k>>6 & 1 )) bit7=$(( k>>7 & 1 ))"
    echo "RES    (AB): ${res}   REMS (90): ${rems}"
    echo "RDLR   (2D): ${lr}   RDFBR (16): ${fbr}   RDEAR (C8): ${ear}"
    echo "RDSPBLK(A7): ${spblk}   RDSPB@0 (E2): ${spb0}   RDDPB@0 (E0): ${dpb0}"
    echo "SFDP   (5A): ${sfdp}"
    echo "SFDP md5: $(echo ${sfdp} | md5sum | cut -c1-12)  signature: $(echo ${sfdp} | cut -c1-11)"
    return 0
}

if [ "$1" = "--me-info" ]; then
    me_info
    exit 0
fi

# ONETREE: --inspect is not in upstream. The facts bios_fw needs when a host
# fails power-good (2026-10-05: seven Quantas with good chips holding foreign
# BMC images sat dark). RAW FACTS ONLY: the decision is triage's
# (biosfw/chip_read.py), where it is tested. Never writes the chip, never
# powers the host, never touches the ME beyond the self-test query.
INSPECT_ME_S=${INSPECT_ME_S:-30}        # how long the ME must stay silent
# The five functions that touch hardware. Everything else in the mode is logic
# and is tested (scripts/test-bios-update-inspect.sh) with these replaced.
insp_take_bus() {       # mux to BMC, flash driver unbound (user mode needs the controller)
    set_gpio_to_bmc >/dev/null 2>&1
    cd /
    if [ -e "$SPI_PATH/$SPI_DEV" ]; then
        echo -n $SPI_DEV > $SPI_PATH/unbind 2>/dev/null
        sleep 1
    fi
    return 0
}
insp_regs() { spi_regs 2>&1; }
insp_bind() {           # bind the flash driver; echoes the read-only device path
    local i n p=""
    echo -n $SPI_DEV > $SPI_PATH/bind 2>/dev/null
    sleep 2
    for i in 1 2 3 4 5; do
        for n in /sys/class/mtd/mtd*/name; do
            [ "$(cat "$n" 2>/dev/null)" = pnor ] && p=$(basename "$(dirname "$n")") && break
        done
        [ -n "$p" ] && break
        sleep 2
    done
    [ -n "$p" ] || return 1
    if [ -e "/dev/${p}ro" ]; then echo "/dev/${p}ro"; else echo "/dev/$p"; fi
}
insp_size() { local d; d=$(basename "$1"); cat "/sys/class/mtd/${d%ro}/size" 2>/dev/null; }
insp_release_bus() {    # unbind, mux to PCH; echoes the GPIO readback ("0" = at PCH)
    local v=absent g=/sys/class/gpio/gpio$GPIO
    [ -e "$SPI_PATH/$SPI_DEV" ] && echo -n $SPI_DEV > $SPI_PATH/unbind 2>/dev/null
    sleep 1
    [ -d "$g" ] || echo $GPIO > /sys/class/gpio/export 2>/dev/null
    if [ -d "$g" ]; then
        echo out > "$g/direction" 2>/dev/null
        echo 0   > "$g/value"     2>/dev/null
        v=$(cat "$g/value" 2>/dev/null)     # read BEFORE the unexport
        echo in  > "$g/direction" 2>/dev/null
        echo $GPIO > /sys/class/gpio/unexport 2>/dev/null
    fi
    echo "${v:-unreadable}"
}
# Test seam: a file of function overrides (root runs this tool; the caller
# already controls its environment).
[ -n "${BIOS_UPDATE_TEST_HOOKS:-}" ] && . "$BIOS_UPDATE_TEST_HOOKS"

if [ "$1" = "--inspect" ]; then
    set +e
    ins() { echo "inspect: $1=$2"; }
    hx() {  # hx <dev> <byte offset> <count> -> "aa bb cc"
        dd if="$1" bs=1 skip="$2" count="$3" 2>/dev/null \
            | hexdump -v -e '1/1 "%02x "' | sed 's/ *$//'
    }
    _insp_bus=0
    insp_done() {
        if [ "${_insp_bus}" = 1 ]; then
            _insp_bus=0
            ins mux "$(insp_release_bus)"
        fi
        rm -f /tmp/.inspect.ff /tmp/.inspect.w
    }
    trap insp_done EXIT
    # A signal ENDS the run: report, exit, and the EXIT trap hands the bus back.
    trap 'ins error signal; exit 143' INT TERM HUP PIPE

    st=$(power_status)
    ins host "${st:-unknown}"
    if [ "${st}" != off ]; then ins error host_not_off; exit 1; fi

    # Any reply at all is "answers". Silent only after INSPECT_ME_S of silence.
    me=""; t=0
    while :; do
        me=$(me_state 2>/dev/null); me=${me%%:*}
        [ "${me}" != silent ] && break
        [ "${t}" -ge "${INSPECT_ME_S}" ] && break
        sleep 3; t=$(( t + 3 ))
    done
    case "${me}" in
        silent) ins me silent ;;
        normal|recovery|other) ins me "${me}"; ins end ok; exit 0 ;;   # bus NOT taken
        *) ins error me_unreadable; exit 1 ;;
    esac

    ins bus taken            # printed first: a dead output channel ends the run here
    _insp_bus=1
    insp_take_bus
    regs=$(insp_regs)
    ins rdid "$(printf '%s\n' "$regs" | sed -n 's/^RDID *(9F): *//p' | head -n 1)"
    # The SFDP basic parameter table starts at 0x30: its first word is bytes 49-52.
    ins sfdp "$(printf '%s\n' "$regs" | sed -n 's/^SFDP *(5A): *//p' | head -n 1 \
        | awk '{print $49, $50, $51, $52}')"

    D=$(insp_bind) || D=""
    if [ -z "$D" ]; then ins error bind_failed; exit 1; fi
    size=$(insp_size "$D")
    case "$size" in ''|*[!0-9]*) ins error chip_absent; exit 1 ;; esac
    if [ "$size" -lt 16777216 ] || [ "$size" -gt 134217728 ]; then ins error chip_absent; exit 1; fi
    ins size "$size"

    # Blank by IDENTITY, never by counting: cmp rc 0 = erased, 1 = content,
    # anything else = error. The reference block is identified by its md5,
    # which triage checks.
    dd if=/dev/zero bs=64k count=1 2>/dev/null | tr '\000' '\377' > /tmp/.inspect.ff
    ins blank_ref "$(md5sum < /tmp/.inspect.ff 2>/dev/null | cut -d' ' -f1)"
    blank_at() {  # blank_at <64k block index> -> 1 | 0 | err
        dd if="$D" bs=64k count=1 skip="$1" 2>/dev/null > /tmp/.inspect.w
        [ "$(wc -c < /tmp/.inspect.w 2>/dev/null)" = 65536 ] || { echo err; return; }
        cmp -s /tmp/.inspect.w /tmp/.inspect.ff
        case $? in 0) echo 1 ;; 1) echo 0 ;; *) echo err ;; esac
    }
    # Sampled: every 4 MiB from block 0, then the last block (reset vector).
    nblk=$(( size / 65536 )); blank=1; i=0
    while [ "$i" -lt "$nblk" ]; do
        case "$(blank_at "$i")" in 1) ;; 0) blank=0; break ;; *) blank=err; break ;; esac
        i=$(( i + 64 ))
    done
    if [ "$blank" = 1 ]; then
        case "$(blank_at $(( nblk - 1 )))" in 1) ;; 0) blank=0 ;; *) blank=err ;; esac
    fi
    ins blank "$blank"

    # Structure of a non-blank chip: RECORDED, never judged. Raw bytes only.
    if [ "$blank" = 0 ]; then
        sig=$(hx "$D" 16 4); f1=""; f2=""; fpt=""
        if [ "$sig" = "5a a5 f0 0f" ]; then
            fb=$(hx "$D" 22 1)                              # FLMAP0 bits 23:16 = FRBA
            case "$fb" in [0-9a-f][0-9a-f])
                frba=$(( 0x$fb << 4 ))
                f1=$(hx "$D" $(( frba + 4 )) 4)             # FLREG1 = BIOS
                f2=$(hx "$D" $(( frba + 8 )) 4)             # FLREG2 = ME
                set -- $f2
                if [ $# -eq 4 ]; then
                    mb=$(( ((0x$2 << 8 | 0x$1) & 0x7fff) << 12 ))
                    [ "$mb" -lt "$size" ] && fpt=$(hx "$D" $(( mb + 16 )) 4)
                fi ;;
            esac
        fi
        ins sig "$sig"; ins flreg1 "$f1"; ins flreg2 "$f2"; ins fpt "$fpt"
    fi
    ins end ok
    exit 0      # the EXIT trap hands the bus back and prints mux=
fi
# end of --inspect

# ONETREE: --read <file> is not in upstream. It runs the same sequence with the
# flashcp replaced by two full-chip reads (compared, so a flaky SPI read cannot
# pass), and restores the host to the power state it was found in.
MODE=flash
NO_POWERON=0
if [ "$1" = "--no-poweron" ]; then
    NO_POWERON=1
    shift
fi
if [ "$1" = "--regs" ]; then
    # ONETREE: --regs is not in upstream. Same sequence, but instead of binding
    # the flash driver it sends four READ-ONLY commands to the chip in the
    # controller's user mode (see spi_regs) and changes nothing on it.
    MODE=regs
    HOST_WAS=$(power_status)
    echo "Bios chip register read started at $(date) (host was: ${HOST_WAS})"
elif [ "$1" = "--read" ]; then
    MODE=read
    OUT_FILE=$2
    if [ -z "$OUT_FILE" ]; then
        echo "usage: $0 [--no-poweron] <image.bin> | --read <out.bin> | --regs | --me-info | --inspect"
        exit 2
    fi
    OUT_FILE=$(readlink -f "$OUT_FILE")
    if [ -e "$OUT_FILE" ]; then
        echo "$OUT_FILE already exists -- not overwriting"
        exit 1
    fi
    touch "$OUT_FILE" && rm -f "$OUT_FILE" || { echo "cannot write $OUT_FILE"; exit 1; }
    HOST_WAS=$(power_status)
    echo "Bios read started at $(date) -> $OUT_FILE (host was: ${HOST_WAS})"
else
# ONETREE: absolute path -- the GPIO helpers below cd into /sys/class/gpio.
IMAGE_FILE=$1
[ -n "$IMAGE_FILE" ] && IMAGE_FILE=$(readlink -f "$IMAGE_FILE")
# ONETREE: installed as /usr/sbin/bios-update, the BMC's update service calls
# this with the unpacked image DIRECTORY (obmc-flash-host-bios@<id>.service:
# ExecStart=/usr/sbin/bios-update /tmp/images/<id>); the image is image-bios.
[ -d "$IMAGE_FILE" ] && IMAGE_FILE="$IMAGE_FILE/image-bios"
if [ -z "$IMAGE_FILE" ]; then
    echo "usage: $0 [--no-poweron] <image.bin> | --read <out.bin> | --regs | --me-info | --inspect"
    exit 2
fi
# ONETREE: refuse before touching anything rather than discover a missing image
# with the host off and the ME in recovery (upstream checks only at flash time).
if [ ! -f "$IMAGE_FILE" ]; then
    echo "Bios image $IMAGE_FILE doesn't exist"
    echo "bios-update: error: image not found: $IMAGE_FILE"
    exit 1
fi

echo "Bios upgrade started at $(date)"
echo "Bios image is $IMAGE_FILE ($(stat -c %s "$IMAGE_FILE") bytes, md5 $(md5sum < "$IMAGE_FILE" | cut -d' ' -f1))"
fi

#Power off host server.
echo "Power off host server"
power_off
sleep 15
if [ "$(power_status)" != "off" ];
then
    echo "Host server didn't power off"
    echo "bios-update: error: host did not power off"
    echo "Bios upgrade failed"
    exit 1
fi
echo "Host server powered off"

#Set ME to recovery mode
# ONETREE: upstream sends the recovery command blind. Here: find out what the
# ME is doing, put it in recovery if it is running, and refuse to touch the
# flash while it is still running normally. Nothing below this block has
# written anything yet, so every abort here leaves the BIOS chip untouched.
echo "Check ME state (up to ${ME_PROBE_S}s for a silent ME to answer)"
ME_BEFORE=$(me_wait "${ME_PROBE_S}" normal recovery)
case "${ME_BEFORE}" in other:*) ME_BEFORE=$(me_wait "${ME_PROBE_S}" normal recovery) ;; esac
echo "ME state: ${ME_BEFORE}"
case "${ME_BEFORE}" in
    recovery)
        ME_PATH="ME was already in recovery"
        ;;
    silent)
        case "${MODE}" in flash) verb=flashing ;; read) verb=reading ;; *) verb="reading registers" ;; esac
        ME_PATH="ME not responding (not running) -- ${verb} without recovery"
        echo "ME is not responding on IPMB: it is not running, so it cannot be" \
             "using the flash. Continuing without the recovery step."
        ;;
    *)
        echo "Set ME to recovery mode"
        rc=0
        ipmb_req 0x2e 0xdf 0x57 0x01 0x00 0x01 || rc=$?
        case "${rc}" in
            0) echo "  ME accepted the recovery request" ;;
            1) echo "  ME answered the recovery request with CC ${IPMB_CC}" ;;
            2) echo "  no reply to the recovery request" ;;
        esac
        sleep 5
        ME_AFTER=$(me_wait "${ME_RECOVERY_WAIT_S}" recovery)
        echo "ME state after recovery request: ${ME_AFTER}"
        case "${ME_AFTER}" in
            recovery)
                ME_PATH="ME put into recovery" ;;
            silent)
                ME_PATH="ME stopped answering after the recovery request -- treated as not running" ;;
            *)
                echo "ME did not enter recovery (state: ${ME_AFTER})."
                echo "bios-update: error: ME did not enter recovery (state: ${ME_AFTER}); nothing written"
                echo "Not flashing: the ME is still running and would be using the chip."
                echo "Nothing was written."
                if { [ "$MODE" = read ] || [ "$MODE" = regs ]; } && { [ "$HOST_WAS" = on ] || [ "$HOST_WAS" = running ]; }; then
                    echo "Powering the host back on (it was ${HOST_WAS})"
                    power_on || true
                else
                    echo "The host is left powered off."
                fi
                echo "Bios upgrade failed"
                exit 1 ;;
        esac
        ;;
esac
echo "ME: ${ME_PATH}"

#Flip GPIO to access SPI flash used by host.
echo "Set GPIO $GPIO to access SPI flash from BMC used by host"
set_gpio_to_bmc

#Bind spi driver to access flash
# ONETREE: on this kernel the controller is already bound at boot (with no
# flash child, the probe ran with the mux at PCH), so bind alone fails;
# unbind first so the rebind re-probes the chip with the mux at BMC.
if [ -e "$SPI_PATH/$SPI_DEV" ]; then
    echo "Unbind spi-aspeed-smc spi driver"
    echo -n $SPI_DEV > $SPI_PATH/unbind
    sleep 1
fi
READ_OK=""
if [ "$MODE" = regs ]; then
    # Driver stays UNBOUND: user mode needs the controller to itself.
    spi_regs && READ_OK=yes || true
else
echo "bind spi-aspeed-smc spi driver"
echo -n $SPI_DEV > $SPI_PATH/bind
sleep 1
fi

#Flashcp image to device.
# ONETREE: upstream walks mtd6/mtd7; onetree's numbering differs, so find the
# MTD named "pnor" (the same name upstream checks for).
PNOR=""
[ "$MODE" = regs ] || for i in 1 2 3 4 5; do
    PNOR=""
    for n in /sys/class/mtd/mtd*/name; do
        [ "$(cat $n)" = "pnor" ] && PNOR=/dev/$(basename $(dirname $n)) && break
    done
    [ -n "$PNOR" ] && break
    sleep 2
done
if [ "$MODE" = regs ]; then
    :
elif [ -n "$PNOR" ] && [ "$MODE" = read ]; then
    want=$(cat /sys/class/mtd/$(basename $PNOR)/size)
    echo "Reading $PNOR ($want bytes) twice..."
    wdt_disarm
    rc=0
    dd if="$PNOR" of="$OUT_FILE" bs=65536 2>/dev/null || rc=$?
    dd if="$PNOR" of="$OUT_FILE.2" bs=65536 2>/dev/null || rc=$?
    wdt_rearm
    s1=$(stat -c %s "$OUT_FILE" 2>/dev/null || echo 0)
    m1=$(md5sum < "$OUT_FILE" | cut -d' ' -f1)
    m2=$(md5sum < "$OUT_FILE.2" | cut -d' ' -f1)
    rm -f "$OUT_FILE.2"
    if [ "$rc" = 0 ] && [ "$s1" = "$want" ] && [ "$m1" = "$m2" ]; then
        READ_OK=yes
        echo "bios read successfully: $OUT_FILE ($s1 bytes, md5 $m1, both reads agree)"
    else
        echo "bios read FAILED (rc=$rc size=$s1/$want md5 $m1 vs $m2) -- $OUT_FILE is not trustworthy"
        mv "$OUT_FILE" "$OUT_FILE.BAD" 2>/dev/null || true
    fi
elif [ -n "$PNOR" ]; then
    echo "Flashing bios image to $PNOR..."
    wdt_disarm
    if flashcp -v "$IMAGE_FILE" "$PNOR"; then
        echo "bios updated successfully..."
        echo "bios-update: flash complete"
    else
        echo "bios update failed..."
        echo "bios-update: error: flashcp failed"
        FLASH_FAILED=1
    fi
    wdt_rearm
else
    echo "pnor not available"
    echo "bios-update: error: pnor MTD partition not found after driver bind"
    FLASH_FAILED=1
fi

#Unbind spi driver
sleep 1
# ONETREE: only if bound -- in --regs mode it never was, and a failed unbind
# under set -e would exit with the mux still at BMC.
if [ -e "$SPI_PATH/$SPI_DEV" ]; then
    echo "Unbind spi-aspeed-smc spi driver"
    echo -n $SPI_DEV > $SPI_PATH/unbind
fi
sleep 10

#Flip GPIO back for host to access SPI flash
echo "Set GPIO $GPIO back for host to access SPI flash"
set_gpio_to_pch
sleep 5

#Reset ME to boot from new bios
echo "Reset ME to boot from new bios"
rc=0
ipmb_req 0x06 0x02 || rc=$?
case "${rc}" in
    0) echo "  ME accepted the cold reset" ;;
    1) echo "  ME answered the cold reset with CC ${IPMB_CC}" ;;
    2) echo "  no reply to the cold reset (the ME may reset before answering, or is not running)" ;;
esac
sleep 10

# ONETREE: not in upstream -- wait for the ME to come back and say how it did.
echo "Waiting up to ${ME_RESET_WAIT_S}s for the ME to report normal operation"
ME_FINAL=$(me_wait "${ME_RESET_WAIT_S}" normal)
me_info || true
case "${ME_FINAL}" in
    normal)   echo "ME: running normally on the new image" ;;
    recovery) echo "ME: STILL IN RECOVERY after the reset -- the host may not power on" ;;
    silent)   echo "ME: NOT RESPONDING after the reset -- the host may not power on" ;;
    *)        echo "ME: unexpected state ${ME_FINAL}" ;;
esac

# ONETREE: a read leaves the host as it found it.
if [ "$MODE" = read ] || [ "$MODE" = regs ]; then
    if [ "$HOST_WAS" = on ] || [ "$HOST_WAS" = running ]; then
        echo "Power on server (it was ${HOST_WAS} before the read)"
        power_on || true
        sleep 10
    else
        echo "Leaving host off (it was ${HOST_WAS} before the read)"
    fi
    echo "Bios ${MODE} finished at $(date)"
    echo "Summary: read ${READ_OK:-FAILED}; ME before: ${ME_BEFORE}; ${ME_PATH}; ME after: ${ME_FINAL}; host: $(power_status)"
    [ -n "$READ_OK" ] && exit 0 || exit 1
fi

# ONETREE: --no-poweron leaves the host off after the flash, so a chip's first
# boot can happen in another blade (ME recovery and cold reset still ran).
if [ "${NO_POWERON}" = 1 ]; then
    echo "Leaving host off (--no-poweron)"
    echo "Bios upgrade finished at $(date)"
    echo "Summary: ME before: ${ME_BEFORE}; ${ME_PATH}; ME after: ${ME_FINAL}; host: $(power_status) (--no-poweron)"
    exit 0
fi

#Power on server
echo "Power on server"
power_on
sleep 5

# Retry to power on once again if server didn't powered on
if [ "$(power_status)" != "on" ] && [ "$(power_status)" != "running" ];
then
    sleep 5
    echo "Powering on server again"
    power_on
fi
# ONETREE: give the host up to 60 s to leave Off, then say what it did in the
# words the fleet verdict (reaper bios-fw-update) reads from the journal.
for _i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    [ "$(power_status)" != "off" ] && break
    sleep 5
done
HOST_STATE=$(busctl get-property xyz.openbmc_project.State.Host \
    /xyz/openbmc_project/state/host0 xyz.openbmc_project.State.Host CurrentHostState 2>/dev/null \
    | awk '{print $2}' | tr -d '"')
echo "bios-update: host state after power-on: ${HOST_STATE:-unknown}"
case "${HOST_STATE:-unknown}" in *Off*|unknown) echo "bios-update: host did not come up" ;; esac
echo "Bios upgrade finished at $(date)"
echo "Summary: ME before: ${ME_BEFORE}; ${ME_PATH}; ME after: ${ME_FINAL}; host: $(power_status)"
[ "${FLASH_FAILED:-0}" = 1 ] && exit 1
exit 0
