#!/bin/bash
#
# wipe_disks.sh -- clear leftover operating systems off a triage node's
# internal disks, so the node can never again boot something it found there.
#
# Why: recycled WD SN810 NVMe drives arrived carrying an HP PC's Windows
# install (BitLocker on et10b4), and with disk ahead of PXE in the BIOS boot
# order the blade booted it and hung (et10b4/et25b4, 2026-10-23..26).
#
# Minimal on purpose (operator, 2026-10-26): remove every partition table and
# filesystem signature and discard the blocks -- no crypto erase, no NVMe
# sanitize/format, no ATA security commands. The media is left unsecured and
# blank: no password, no encryption key, nothing to unlock.
#
# Only when needed: a disk with no partition table and no signature is left
# untouched, so a re-inventory of a clean node costs nothing.
#
# Never touched: removable or USB disks (the live media, a memtest stick),
# anything mounted, anything held by md/lvm/dm, and transports other than
# nvme/sata/sas/ata.
#
# post.sh runs this for action=inventory only, and only when the PXE config
# for the VLAN passed flaxwipe=1 (triage VLANs by default). It writes its
# report to stdout (post.sh keeps it as diskwipe.txt) and always exits 0:
# a failed wipe must not stop the inventory from reaching the bang.
#
# Report lines (one per disk, grep-able):
#   WIPED <dev> model=<m> serial=<s> size=<n> method=<how> verified=<yes|NO>
#   CLEAN <dev> ... (nothing to wipe)
#   SKIP  <dev> ... reason=<why>
#   BOOTENTRY-REMOVED Boot<XXXX> <label>
#
# WIPE_DRYRUN=1 prints what would be done and changes nothing.

DRY="${WIPE_DRYRUN:-0}"
EFI_DIR="${WIPE_EFI_DIR:-/sys/firmware/efi}"
SYS_BLOCK="${WIPE_SYS_BLOCK:-/sys/block}"

run() {
    if [ "$DRY" = 1 ]; then echo "  dry-run: $*"; return 0; fi
    "$@"
}

field() {   # field <dev> <lsblk column>, whole-device value only
    lsblk -dno "$2" "$1" 2>/dev/null | sed -e 's/^ *//' -e 's/ *$//' -e 's/ /_/g'
}

held() {    # md/lvm/dm holders on the disk or any of its partitions
    local name=${1#/dev/} h
    for h in "$SYS_BLOCK/$name"/holders "$SYS_BLOCK/$name"/"$name"*/holders; do
        [ -d "$h" ] && [ -n "$(ls -A "$h" 2>/dev/null)" ] && return 0
    done
    return 1
}

echo "wipe_disks.sh $(date -u +%FT%TZ) dryrun=$DRY"
echo "== before"
lsblk -o NAME,TYPE,TRAN,RM,SIZE,MODEL,SERIAL,FSTYPE,LABEL,PARTUUID 2>&1

PU=$(mktemp)   # PARTUUIDs of wiped disks; a file, since the loop is a subshell
lsblk -dnpo NAME,TYPE 2>/dev/null | while read -r dev type; do
    [ "$type" = disk ] || continue
    tran=$(field "$dev" TRAN); rm=$(field "$dev" RM)
    model=$(field "$dev" MODEL); serial=$(field "$dev" SERIAL); size=$(field "$dev" SIZE)
    id="$dev model=${model:--} serial=${serial:--} size=${size:--}"
    case "$tran" in
        nvme|sata|sas|ata) ;;
        *)  # unattached nbd/zram etc. are 0-byte "disks": not worth a line
            [ "$size" = "0B" ] || echo "SKIP  $id reason=transport:${tran:-none}"
            continue ;;
    esac
    if [ "$rm" = 1 ]; then echo "SKIP  $id reason=removable"; continue; fi
    if lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep -q .; then
        echo "SKIP  $id reason=mounted"; continue
    fi
    if held "$dev"; then echo "SKIP  $id reason=in-use-by-md/lvm/dm"; continue; fi

    parts=$(lsblk -nrpo NAME,TYPE "$dev" 2>/dev/null | awk '$2=="part"{print $1}')
    sigs=$(wipefs -n "$dev" 2>/dev/null | awk 'NR>1')
    if [ -z "$parts" ] && [ -z "$sigs" ]; then
        echo "CLEAN $id"; continue
    fi

    # PARTUUIDs, so the UEFI boot entries that point into this disk can go too.
    for p in $parts; do
        u=$(lsblk -dno PARTUUID "$p" 2>/dev/null | tr 'A-F' 'a-f')
        [ -n "$u" ] && echo "$u" >> "$PU"
    done

    method="wipefs"
    for p in $parts; do run wipefs -a -q "$p"; done
    run wipefs -a -q "$dev"
    if [ "$(field "$dev" DISC-MAX)" != "0B" ] && [ -n "$(field "$dev" DISC-MAX)" ]; then
        if run blkdiscard -f "$dev"; then
            method="wipefs+discard"
        else
            method="wipefs+zero"
        fi
    else
        method="wipefs+zero"
    fi
    if [ "$method" = "wipefs+zero" ]; then
        # No discard: zero the first and last 16 MiB, where the GPT copies and
        # most boot loaders live.
        bytes=$(blockdev --getsize64 "$dev" 2>/dev/null || echo 0)
        run dd if=/dev/zero of="$dev" bs=1M count=16 conv=fsync status=none
        [ "$bytes" -gt $((32 * 1048576)) ] && \
            run dd if=/dev/zero of="$dev" bs=1M count=16 seek=$((bytes / 1048576 - 16)) conv=fsync status=none
    fi
    run blockdev --rereadpt "$dev" 2>/dev/null
    udevadm settle 2>/dev/null

    verified=yes
    if [ "$DRY" != 1 ]; then
        [ -n "$(wipefs -n "$dev" 2>/dev/null | awk 'NR>1')" ] && verified=NO
        lsblk -nrpo TYPE "$dev" 2>/dev/null | grep -q '^part$' && verified=NO
    else
        verified=dry-run
    fi
    echo "WIPED $id method=$method verified=$verified"
done

# Boot entries pointing at a wiped partition: remove them, so a disk can
# never again be put ahead of PXE by a stale "Windows Boot Manager".
if [ -s "$PU" ] && [ -d "$EFI_DIR" ]; then
    efibootmgr -v 2>/dev/null | grep -E '^Boot[0-9A-Fa-f]{4}' | while IFS= read -r line; do
        low=$(echo "$line" | tr 'A-F' 'a-f')
        while read -r u; do
            case "$low" in
                *"$u"*)
                    num=$(echo "$line" | sed -E 's/^Boot([0-9A-Fa-f]{4}).*/\1/')
                    label=$(echo "$line" | sed -E 's/^Boot[0-9A-Fa-f]{4}\*? //' | cut -f1)
                    run efibootmgr -q -b "$num" -B
                    echo "BOOTENTRY-REMOVED Boot$num $label"
                    break ;;
            esac
        done < "$PU"
    done
fi
rm -f "$PU"

echo "== after"
lsblk -o NAME,TYPE,TRAN,RM,SIZE,MODEL,SERIAL,FSTYPE,LABEL,PARTUUID 2>&1
[ -d "$EFI_DIR" ] && efibootmgr 2>&1
exit 0
