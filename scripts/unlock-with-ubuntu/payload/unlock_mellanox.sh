#!/bin/bash
# unlock_mellanox.sh -- the unlock station's flash pass.
#
# Derived from roles/apply_pxe_payloads/files/post/update_mellanox.sh, with
# four deliberate differences. Do not "fix" these back toward the original:
#
#   1. NO NETWORK. Images come from the bundle's ./fw/<PSID>/ instead of
#      curl http://bang/... -- a jumpered mezzanine card passes no traffic, and
#      the deployed station has no other NIC at all.
#   2. NO secure-fw SKIP. The original skips any card reporting secure-fw
#      because mstflint cannot burn it. Clearing that lock -- with the override
#      jumper fitted -- is this station's entire purpose.
#   3. The PSID->image map is DATA (nic_fw_map.tsv), extracted from the
#      original's FWMAP at build time. Never hand-edit it here; fix FWMAP and
#      rebuild, or the station and the fleet disagree about what a card should be.
#   4. Ends in one of three rack signals (power state + IDENT), because that
#      is all an operator reliably notes. Solid blue is power-on and means
#      nothing about this script. REVISED 2026-09-22 (operator decision): the
#      old "pass/fail not distinguishable at the rack" design let a card that
#      was never unlocked, or never got its UEFI ROM, go back into service
#      looking done.
#        DONE     IDENT blinking            -> card finished, pull it
#        FLIP     dark (off, no blink)      -> flip the jumper state, run again
#        PROBLEM  on, no blink, after ~15m  -> read SOL (jumper-less only)
#      The verdict and recent RESULT lines are also printed to /dev/console
#      and left in /etc/issue.d, so Enter in SOL shows them.
# NO `set -u`. common_mellanox.sh is shared verbatim with the post lane and its
# domstflint() assigns binfile=$3 while getdevinfo() calls it with two args --
# fatal under -u. That cost the first three live runs on 2026-09-18: every card
# query died, nothing was flashed, and IDENT still blinked "done", so a card
# went back into service still locked. update_mellanox.sh has always run
# without -u; the station matches it. See tests/test_unlock_shell_compat.py.

here=$(cd "$(dirname "$0")" && pwd)
cd "$here" || exit 1

logdir="${MEZZ_LOGDIR:-/var/log/flax/mezz-flash}"
mkdir -p "$logdir"
log="$logdir/$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$log") 2>&1

echo "=== unlock_mellanox.sh $(date -u +%FT%TZ) ==="
[ -f MANIFEST ] && cat MANIFEST

# PROTECT_PCI is read by mezz_select.py; SHUTDOWN_ON_DONE is read here.
[ -f /etc/flax/mezz-flash.conf ] && . /etc/flax/mezz-flash.conf

# Where the operator-facing verdict goes. /dev/console is whatever the LAST
# console= on the kernel command line names -- the SOL port on every blade
# type we install (ttyS1 on Leopard), without this script knowing which.
soldev="${MEZZ_SOL_DEV:-/dev/console}"
# agetty --reload (mezz-flash-banner.timer, every 20s) skips the redraw when
# the rendered issue text is unchanged -- measured on et9b1, 0 redraws in 35s.
# A SOL session opened after the run has no scrollback, so the banner has to
# repaint on every tick for a reattach to see it. \d and \t are agetty's own
# date/time escapes, expanded at every render, so this line makes every reload
# a redraw. Issue file only: the console copy gets no escapes.
clockline='(redrawn \d \t UTC -- refreshes every 20s)'
issuefile="${MEZZ_ISSUE:-/etc/issue.d/mezz-flash.issue}"
# Machine-readable state for a triage mezz_flash agent (see writestatus).
statusfile="${MEZZ_STATUS:-/run/flax/mezz-flash-status.json}"
# Which bundle this station runs, for the agent and for the log.
bundlever=$(awk '/^repo/ {print $3}' MANIFEST 2>/dev/null)
bundlever="${bundlever:-unknown}"

# The serial getty is up before this finishes, and Enter in SOL reprints the
# issue file -- which still holds the PREVIOUS card's verdict. Replace it first,
# or an operator who swapped cards reads the old card's DONE mid-run.
# The card this run is working on, one key per line (operator decision
# 2026-09-22): what matters now reads as a list at the top of the screen, not
# as one dense key=value row lost among single-line log noise.
function cardfields()
{
    local verdicttext=$1
    [ -n "$devmac" ] || return 0
    printf "  %-13s %s\n" "Card MAC:" "${devmac:-unknown}"
    printf "  %-13s %s\n" "Part:" "${devopn:-unknown}"
    printf "  %-13s %s\n" "PSID:" "${devpsid:-unknown}"
    printf "  %-13s %s\n" "Firmware:" "${devfwver:-unknown}"
    if [ "$devsecure" = "secure-fw" ]; then
        printf "  %-13s %s\n" "Lock:" "LOCKED (secure-fw)"
    else
        printf "  %-13s %s\n" "Lock:" "cleared"
    fi
    if [ "$cardlivefish" = "yes" ]; then
        printf "  %-13s %s\n" "Jumper:" "fitted (livefish)"
    else
        printf "  %-13s %s\n" "Jumper:" "not fitted"
    fi
    printf "  %-13s %s\n" "Burned:" "${cardburned:-no}"
    case "${cardbootrom:-unknown}" in
        already) printf "  %-13s %s\n" "Boot ROM:" "already on (UEFI+PXE+legacy)" ;;
        set)     printf "  %-13s %s\n" "Boot ROM:" "set: ${cardromset:-none}" ;;
        pending) printf "  %-13s %s\n" "Boot ROM:" "pending (needs the other jumper state)" ;;
        failed)  printf "  %-13s %s\n" "Boot ROM:" "FAILED" ;;
        *)       printf "  %-13s %s\n" "Boot ROM:" "${cardbootrom:-unknown}" ;;
    esac
    printf "  %-13s %s\n" "Verdict:" "$verdicttext"
}

# Every RESULT row from earlier runs, newest first, as TSV:
#   sig  when  mac  opn  psid  fw  lock  burn  bootrom  verdict  verified
# `sig` is everything except the timestamp, so repeats of an unchanged state
# collapse (operator decision 2026-09-22: five identical rows say nothing five
# times -- keep the newest and count the rest). Both the banner table and the
# status file's history[] are built from this.
function historyraw()
{
    local f stamp
    for f in $(ls -t "$logdir"/*.log 2>/dev/null); do
        [ "$f" = "$log" ] && continue
        stamp=$(basename "$f" .log)
        grep " RESULT " "$f" 2>/dev/null | tac | awk -v s="$stamp" '
        {
            mac=""; opn=""; psid=""; fw=""; sec=""; burn=""; rom=""; verd="";
            for (i = 1; i <= NF; i++) {
                split($i, kv, "=");
                if (kv[1] == "mac") mac = kv[2];
                else if (kv[1] == "opn") opn = kv[2];
                else if (kv[1] == "psid") psid = kv[2];
                else if (kv[1] == "fw") fw = kv[2];
                else if (kv[1] == "sec") sec = kv[2];
                else if (kv[1] == "burned") burn = kv[2];
                else if (kv[1] == "bootrom" || kv[1] == "uefi") rom = kv[2];
                else if (kv[1] == "verdict") verd = kv[2];
            }
            gsub(/\[|\]/, "", sec);
            ver = (verd == "") ? "false" : "true";
            if (verd == "") verd = "unknown";
            lock = (sec == "secure-fw") ? "locked" : "cleared";
            sig = mac "|" opn "|" psid "|" fw "|" lock "|" burn "|" rom "|" verd;
            print sig "\t" s "\t" mac "\t" opn "\t" psid "\t" fw "\t" lock \
                  "\t" burn "\t" rom "\t" verd "\t" ver;
        }'
    done
}

# Earlier runs as a table: one heading row, then values only. Newest first,
# unchanged repeats collapsed with an xN count, and narrow enough for an
# 80-column SOL screen. A row from a pre-fix log (no verdict= field) is
# starred -- those logged burned=yes / uefi=set without checking anything.
function historytable()
{
    local rows
    rows=$(historyraw | awk -F'\t' '
        function short(v) {
            if (v == "remove-jumper") return "rm-jump";
            if (v == "fit-jumper") return "fit-jump";
            if (v == "unknown") return "?";
            return v;
        }
        { if (!($1 in rec)) { order[++n] = $1; rec[$1] = $0 } cnt[$1]++ }
        END {
            # Oldest first: the newest row ends up next to the current
            # card, and the status line below it is what stays on screen.
            last = (n < 5) ? n : 5;
            for (i = last; i >= 1; i--) {
                split(rec[order[i]], f, "\t");
                when = substr(f[2], 5, 4) "-" substr(f[2], 10, 4);
                lock = (f[7] == "locked") ? "LOCK" : "ok";
                verd = short(f[10]) ((f[11] == "true") ? "" : "*");
                runs = (cnt[order[i]] > 1) ? "x" cnt[order[i]] : "";
                printf "  %-9s %-12s %-12.12s %-10.10s %-4s %-4.4s %-7.7s %-8.8s %-4s\n",
                       when, f[3], f[4], f[6], lock, f[8], f[9], verd, runs;
            }
        }')
    [ -n "$rows" ] || return 0
    printf "  %-9s %-12s %-12s %-10s %-4s %-4s %-7s %-8s %-4s\n" \
           WHEN MAC PART FW LOCK BURN BOOTROM VERDICT RUNS
    printf "%s\n" "$rows"
    # grep, not a case on the whole blob: the starred row is rarely the last.
    printf "%s\n" "$rows" | grep -q "\*" \
        && echo "  * pre-fix log: burned/boot ROM were never checked"
}

# The same collapsed rows as JSON, for the status file. A jumpered run is
# invisible while it happens -- no SOL, no BMC, no NIC -- so its outcome can
# only be reported on the NEXT jumper-less boot, and the agent needs these.
# `verified` is false for a pre-fix log, which recorded burned/boot ROM
# without checking either; `runs` counts collapsed repeats.
function historyjson()
{
    historyraw | awk -F'\t' '
        { if (!($1 in rec)) { order[++n] = $1; rec[$1] = $0 } cnt[$1]++ }
        END {
            out = "";
            for (i = 1; i <= n && i <= 5; i++) {
                split(rec[order[i]], f, "\t");
                out = out (i > 1 ? "," : "") \
                  "{\"when\":\"" f[2] "\",\"mac\":\"" f[3] "\",\"opn\":\"" f[4] \
                  "\",\"psid\":\"" f[5] "\",\"fw\":\"" f[6] "\",\"lock\":\"" f[7] \
                  "\",\"burned\":\"" f[8] "\",\"bootrom\":\"" f[9] \
                  "\",\"verdict\":\"" f[10] "\",\"verified\":" f[11] \
                  ",\"runs\":" cnt[order[i]] "}";
            }
            print out;
        }'
}

# One card as JSON, for the status file a triage agent reads. Values are
# tokens from mstflint/mstconfig or our own verdicts, so no escaping is needed.
function cardjsonobj()
{
    local lock jump
    lock=cleared; [ "$devsecure" = "secure-fw" ] && lock=locked
    jump=no; [ "$cardlivefish" = "yes" ] && jump=yes
    printf '{"mac":"%s","opn":"%s","psid":"%s","fw":"%s","lock":"%s","jumper":"%s","burned":"%s","bootrom":"%s","romset":"%s","verdict":"%s"}' \
        "${devmac:-unknown}" "${devopn:-unknown}" "${devpsid:-unknown}" \
        "${devfwver:-unknown}" "$lock" "$jump" "${cardburned:-no}" \
        "${cardbootrom:-unknown}" "${cardromset:-none}" "$1"
}

# The status file. A jumper-less run leaves the node on the network, so an
# agent can read this instead of scraping SOL; a jumpered card leaves both the
# BMC and the NIC dark, and then the rack signal is all there is.
# Written at every progress step too, so a tile shows RUNNING during a burn
# rather than going blank for three minutes.
function writestatus()
{
    local state=$1 text=$2 cards=$3
    local tmp="${statusfile}.tmp"
    mkdir -p "$(dirname "$statusfile")" 2>/dev/null
    printf '{"ts":"%s","state":"%s","text":"%s","station":true,"bundle":"%s","host":"%s","cards":[%s],"history":[%s]}\n' \
        "$(date -u +%FT%TZ)" "$state" "$text" "$bundlever" "$(hostname)" \
        "$cards" "$(historyjson)" \
        > "$tmp" 2>/dev/null && mv -f "$tmp" "$statusfile" 2>/dev/null
    # Same content next to the logs: /run is tmpfs and empty after a reboot.
    cp -f "$statusfile" "$logdir/status.json" 2>/dev/null || true
}

# Order matters on a SOL console that scrolls (operator decision
# 2026-09-22): title, then the history table oldest first, then the card in
# hand, and the status line LAST -- so the one line that must not scroll off
# is the last thing written.
function bannerbody()
{
    echo "=================== MEZZ FLASH STATION ==================="
    echo "--- earlier runs (oldest first) ---"
    historytable
    echo
    printf "%s" "$cardblocks"
    cardfields "$3"
    echo
    echo "$1"
    [ -n "$2" ] && echo "$2"
    echo "=========================================================="
}

#
# The station does not hold boot (Type=simple), so the prompt is up while it
# works: progress() keeps the banner saying what it is doing right now.
function progress()
{
    mkdir -p "$(dirname "$issuefile")" 2>/dev/null
    writestatus RUNNING "$1" "$cardjson"
    { bannerbody "$(date -u +%FT%TZ)  RUNNING  $1" \
                 "Leave the card in until the verdict." "in progress"
      echo "$clockline"; } > "$issuefile" 2>/dev/null || true
    # Enter at `login:` only reprints the prompt, never the issue file (tested
    # on et9b1). --reload makes the waiting getty redraw WITH it, so push it.
    agetty --reload >/dev/null 2>&1 || true
}
progress "starting; looking for Mellanox cards."

# A DONE blink from the previous run survives a host power cycle on the BMC.
# Clear it now so the only blink the operator can see is THIS run's verdict.
ipmitool chassis identify 0 >/dev/null 2>&1 || true

# globals consumed by common_mellanox.sh's domstflint
allow_psid_change=1
no_fw_ctrl=0
needbmcreset=0
# Did this run change any card? Only a run that did may power the node off.
# A no-op run (every card already at target, unlocked, UEFI on) leaves it up,
# so a node with a finished card can still be booted to investigate things.
# Operator decision 2026-09-22, after et9b1 went dark on a no-op pass.
nicchanged=0

source ./common_mellanox.sh

# --- map ------------------------------------------------------------------
declare -A FWMAP=()
while IFS=$'\t' read -r psid fwdir fwbin; do
    [ -n "${psid:-}" ] || continue
    FWMAP["$psid"]="${fwdir}|${fwbin}"
done < nic_fw_map.tsv
echo "map: ${#FWMAP[@]} PSIDs"

# OPN -> PSID, built at bundle time from the mellanox share's symlinks
# (MCX4411A-ACQ -> MT_2450112034). The fallback route for a card whose PSID
# has no FWMAP row: only three OEM-branded PSIDs are catalogued, but any card
# still reports its own part number.
declare -A OPNMAP=()
if [ -f nic_opn_map.tsv ]; then
    while IFS=$'\t' read -r opn opnpsid; do
        [ -n "${opn:-}" ] || continue
        OPNMAP["$opn"]="$opnpsid"
    done < nic_opn_map.tsv
fi
echo "opn map: ${#OPNMAP[@]} OPNs"
if [ "${#FWMAP[@]}" -eq 0 ]; then
    echo "FATAL: empty nic_fw_map.tsv -- bad bundle, flashing nothing."
    exit 1
fi

# --- which cards ----------------------------------------------------------
# Cards holding an IPv4 address are skipped: on a development station the PCIe
# uplink is a ConnectX too, and resetting it mid-run drops the operator's ssh
# session. Not a safety gate -- a flashed uplink lands at the same FW+UEFI as
# everything else -- just a convenience. MEZZ_FLASH_ALL=1 disables it.
if [ "${MEZZ_FLASH_ALL:-0}" = "1" ]; then
    targets=$(lspci -d "15b3:" | cut -d " " -f1 | grep '00\.0$')
    echo "MEZZ_FLASH_ALL=1 -- every Mellanox card is a target"
else
    targets=$(python3 ./mezz_select.py)
fi
echo "targets: ${targets:-<none>}"
progress "cards: ${targets:-none found}"
if [ -z "$targets" ]; then
    echo "No target cards. Nothing to flash."
fi

# --- flash ----------------------------------------------------------------
# Jumper fitted <=> the card is in livefish. Measured over 40 live runs on
# et9b1/et9b4 (2026-09-21/22), and it decides everything below:
#   livefish:  mstflint burns fine, but mstconfig can write NOTHING ("Device in
#              Livefish mode is not supported") and mstfwreset fails with
#              ME_MAD_SEND_FAILED(8). 27/27 jumpered runs.
#   no jumper: mstconfig works, but burning a locked card is always refused
#              ("Changing PSID is unsupported under controlled FW"). 13/13.
# So unlocking takes TWO passes -- a jumpered burn, then a jumper-less pass
# that sets the UEFI ROM, which the unlocked image defaults to OFF -- and the
# station has to say which one a card needs next. mstflint's query reads the
# same either way, so it cannot tell them apart; mstconfig can.
function cardmode()
{
    if mstconfig -d "$1" q 2>&1 | grep -qi "livefish"; then
        cardlivefish=yes
    else
        cardlivefish=no
    fi
}

# common_mellanox.sh's getdevinfo() greps an UNANCHORED 'PSID:', which also
# matches the 'Orig PSID:' line mstflint prints once a card's PSID has been
# changed -- so $devpsid comes back holding BOTH values, newline-joined. Every
# later comparison then fails: the FWMAP lookup misses and an already-unlocked
# card reads as unknown. Re-derive it from the same query output with an
# anchored match. common_mellanox.sh is shared verbatim with the post lane and
# is not ours to edit (see the header), so the narrowing happens here.
function narrowpsid()
{
    local only
    only=$(printf "%s\n" $devinfo | grep '^PSID:' | cut -d':' -f2 | sed 's/^\s*//')
    [ -n "$only" ] && devpsid="$only"
}

# Who this card IS. A BDF names the SLOT -- every card the station handles sits
# in the same one -- so a log keyed on it cannot be read back as a history:
# reviewing 2026-09-21 showed four burn attempts at 5e:00.0 with no way to tell
# one card retried four times from four cards. mstflint already reports the
# identity in the query getdevinfo() parses, so this costs one more grep.
function cardident()
{
    devmac=$(printf "%s\n" $devinfo | grep '^Base MAC:' | awk '{print $3}')
    devguid=$(printf "%s\n" $devinfo | grep '^Base GUID:' | awk '{print $3}')
    devmac="${devmac:-unknown}"
    devguid="${devguid:-unknown}"
}

# The card's own part number, which it reports regardless of how its PSID is
# branded. mstconfig prints it with a hardware revision suffix
# (MCX4411A-ACQ_Ax); the share's OPN symlinks are the bare form.
function cardopn()
{
    local name
    name=$(mstconfig -d "$1" q 2>/dev/null | grep '^Name:' | awk '{print $2}')
    devopn="${name%%_*}"
    devopn="${devopn:-unknown}"
}

# What reset levels does this card actually offer RIGHT NOW? mstfwreset's own
# query subcommand answers it, read-only.
#
# Captured at two moments, because only the pair is evidence. The post lane
# finishes a card in one boot -- burn, mstfwreset, sleep 5, UEFI tweak, no
# reboot -- and the station cannot, but we do not know whether that is inherent
# to a just-burned LOCKED card or something about how we ask. post never meets
# the question because it refuses locked cards outright (update_mellanox.sh:177).
#
# The only reading we have is from an already-unlocked card, which reports
# levels 3 and 4 supported. A locked card's baseline has never been captured.
# If the post-failure reading comes back with nothing supported, the cold power
# cycle is inherent and no --level choice buys a single pass; if it still
# offers a level, it is worth trying that level before accepting two passes.
function resetlevels()
{
    local mlxdev=$1
    local when=$2
    echo "--- reset-levels ($when) $mlxdev ---"
    mstfwreset -d "$mlxdev" query 2>&1 | sed 's/^/    /'
    echo "--- end reset-levels ($when) ---"
}

# The burn pass. Every "we cannot burn this" exit is a `return`, never the
# loop's `continue`: a card we have no image for is still a card that has to
# PXE boot when it goes into service, so it must still reach the UEFI pass.
# That coupling is exactly what cost et26b3 its UEFI ROM on 2026-09-21.
function burnpass()
{
    local mlxdev=$1
    local entry fwdir fwbin img want have bininfo binfwver binpsid viapsid
    local burnlog resetlog burnrc

    if [ "$cardlivefish" = "no" ] && [ "$devsecure" = "secure-fw" ]; then
        echo "$mlxdev: locked and NO jumper -- controlled FW refuses the PSID" \
             "change every time; not burning. Fit the jumper and run again."
        cardburned=needs-jumper
        return 0
    fi

    entry="${FWMAP[$devpsid]:-}"
    if [ -z "$entry" ]; then
        # No FWMAP row -- an OEM lock nobody has catalogued. Do not walk away:
        # the card reports its own OPN and the share resolves that to an image
        # the bundle already carries. Operator decision 2026-09-21: resolve and
        # burn rather than skip and wait for a map row to be added.
        viapsid="${OPNMAP[$devopn]:-}"
        [ -n "$viapsid" ] && entry="${FWMAP[$viapsid]:-}"
        if [ -z "$entry" ]; then
            echo "$mlxdev: PSID $devpsid not in map and OPN $devopn does not" \
                 "resolve to a bundled image; no burn."
            cardnoimage=yes
            return 0
        fi
        echo "$mlxdev: PSID $devpsid not in map; resolved by OPN $devopn -> $viapsid"
    fi

    fwdir="${entry%%|*}"
    fwbin="${entry##*|}"
    img="fw/${fwdir}/${fwbin}"
    if [ ! -f "$img" ]; then
        echo "$mlxdev: image missing from bundle ($img); no burn."
        cardnoimage=yes
        return 0
    fi
    # The rootfs takes a hard power cut every cycle (blade pulled while up), so
    # a truncated image is a real possibility, not a theoretical one.
    want=$(awk -v f="$img" '$2 == f {print $1}' SHA256SUMS)
    have=$(sha256sum "$img" | cut -d' ' -f1)
    if [ -z "$want" ] || [ "$want" != "$have" ]; then
        echo "$mlxdev: checksum FAILED for $img (want=${want:-<absent>} have=$have); refusing to burn."
        cardburned=failed
        return 0
    fi

    binfwver=""; binpsid=""
    IFS=$'\n' bininfo=$(mstflint -i "$img" query)
    binfwver=$(printf "%s\n" $bininfo | grep 'FW Version:' | cut -d':' -f2 | sed 's/^\s*//')
    binpsid=$(printf "%s\n" $bininfo | grep 'PSID:' | cut -d':' -f2 | sed 's/^\s*//')
    echo "$mlxdev: dev fw=$devfwver psid=$devpsid sec=[$devsecure] | img fw=$binfwver psid=$binpsid"

    # post's needsverup() compares FW and PSID only, which is right there --
    # it refuses locked cards outright, so it never meets one that is at the
    # target image and STILL locked. The station is the only place that happens,
    # and it is exactly what a FAILED unlock looks like: the burn landed, the
    # activation did not, and the card came back re-branded but locked. Walking
    # away from it would strand the one card that most needs another pass.
    if [ "$devfwver" == "$binfwver" ] && [ "$devpsid" == "$binpsid" ]; then
        if [ "$devsecure" != "secure-fw" ]; then
            echo "$mlxdev: already at target FW+PSID and unlocked; no burn."
            return 0
        fi
        echo "$mlxdev: at target FW+PSID but STILL secure-fw -- re-burning to clear the lock."
    fi

    # A locked card's reset-level baseline, taken while it is still locked and
    # unburned. Never captured before, and the post-failure reading below means
    # nothing without it -- it could not distinguish a card that LOST the
    # capability from one that never advertised it.
    resetlevels "$mlxdev" "pre-burn"

    # mstflint reports the FW boot address during the BURN, before mstfwreset
    # is ever called, so that -- not the reset -- is what decides whether a
    # single pass is possible. Grade it without losing the live log.
    burnlog=$(mktemp)
    progress "BURNING firmware on card $devmac -- DO NOT PULL (about 3 min)."
    domstflint burn "$mlxdev" "$img" 2>&1 | tee "$burnlog"
    burnrc=${PIPESTATUS[0]}
    # The old code never looked: every refused burn was logged burned=yes.
    if [ "$burnrc" -ne 0 ] || grep -q -- "^-E-" "$burnlog"; then
        echo "$mlxdev: WARNING burn FAILED (rc=$burnrc); the card is unchanged."
        cardburned=failed
        rm -f "$burnlog"
        return 0
    fi
    cardburned=yes
    nicchanged=1
    if grep -q "Failed to update FW boot address" "$burnlog"; then
        cardbootaddr=failed
    else
        cardbootaddr=ok
    fi
    rm -f "$burnlog"

    # mstfwreset's exit status is the difference between "the new image is
    # running" and "the new image is merely in flash". It fails on these cards
    # with ME_MAD_SEND_FAILED(8) -- mstflint says so itself ("Failed to update
    # FW boot address. Power cycle the device in order to load the new FW") --
    # and the old code ignored it and carried on as though the card had
    # activated. It had not: the config the UEFI pass then read still belonged
    # to the OUTGOING image. Record it so the log can say the card needs
    # another pass rather than leaving that to be inferred from a warning
    # buried in mstflint's output.
    resetlog=$(mktemp)
    domstfwreset "$mlxdev" > "$resetlog" 2>&1
    cardresetrc=$?
    cat "$resetlog"
    rm -f "$resetlog"
    if [ "$cardresetrc" -eq 0 ]; then
        cardactivated=yes
    else
        cardactivated=no
        echo "$mlxdev: WARNING mstfwreset failed (rc=$cardresetrc) -- new FW is"
        echo "$mlxdev: in flash but NOT running. The card needs a cold power"
        echo "$mlxdev: cycle, then a second station pass to finish (the config"
        echo "$mlxdev: read below still belongs to the OUTGOING image)."
        resetlevels "$mlxdev" "post-failure"
    fi
    sleep 5
    # The record that proves the lock cleared: Security Attributes should no
    # longer carry secure-fw once the unlocked image is on the card.
    getdevinfo "$mlxdev"
    narrowpsid
    cardident
    cardopn "$mlxdev"
    echo "$mlxdev: POST-FLASH fw=$devfwver psid=$devpsid sec=[$devsecure]"
}

# The UEFI expansion ROM pass -- the card must PXE boot once it goes into
# service, and the fleet standard is UEFI, not legacy-PXE-only.
#
# Fails CLOSED. An unreadable config query is the case with the LEAST evidence
# the ROM is on, so it must set it rather than assume it. The old
# `${uefival:-1}` had that exactly backwards, and it is how the 06:13-07:41
# runs on et26b3 passed over a card whose ROM was off: mstfwreset had failed
# with ME_MAD_SEND_FAILED(8), so the config being read still belonged to the
# outgoing locked image. `mstconfig set` writes the Next Boot column and is
# idempotent, so re-setting an already-enabled card costs nothing.
# The boot-ROM settings every card must leave with, as key:wanted:set-arg:tag.
# Only UEFI x86 has been seen wrong -- the unlocked MT_2450112034 image
# defaults it False(0) -- but operator decision 2026-09-22 is to enforce all
# three: one set, one read-back, and an image or OEM default we have not met
# cannot ship a card that will not PXE boot. LEGACY_BOOT_PROTOCOL is for
# legacy-BIOS PXE only; it keeps both boot modes working.
BOOTROM_KEYS="EXP_ROM_UEFI_x86_ENABLE:1:EXP_ROM_UEFI_x86_ENABLE=true:UEFI
EXP_ROM_PXE_ENABLE:1:EXP_ROM_PXE_ENABLE=true:PXE
LEGACY_BOOT_PROTOCOL:1:LEGACY_BOOT_PROTOCOL=PXE:LEGACY"

# The number in parens that mstconfig prints for one key: True(1) -> 1,
# PXE(1) -> 1. Empty when the key is absent or the query failed.
function romvalue()
{
    printf "%s\n" "$1" | grep -E "^\s+$2\s" \
        | sed -re "s/^\s+$2\s+\S+\(([0-9]+)\)\s*$/\1/"
}

# From one query's output, which settings are not yet what we want.
# -> romargs (for mstconfig set) and romtags (for the log). Fails CLOSED: an
# unreadable key counts as wrong, so an unreadable query sets all of them.
function romneeds()
{
    local key want arg tag
    romargs=""
    romtags=""
    while IFS=: read -r key want arg tag; do
        [ -n "$key" ] || continue
        if [ "$(romvalue "$1" "$key")" != "$want" ]; then
            romargs="$romargs $arg"
            romtags="${romtags:+$romtags,}$tag"
        fi
    done <<< "$BOOTROM_KEYS"
    romargs="${romargs# }"
}

# What this card needs next, from what the two passes found.
function cardverdict()
{
    if [ "$cardburned" = "failed" ] || [ "$cardbootrom" = "failed" ]; then
        echo problem
    elif [ "$cardnoimage" = "yes" ] && \
         { [ "$cardlivefish" = "yes" ] || [ "$devsecure" = "secure-fw" ]; }; then
        echo problem
    elif [ "$cardlivefish" = "yes" ]; then
        echo remove-jumper
    elif [ "$devsecure" = "secure-fw" ]; then
        echo fit-jumper
    elif [ "$cardbootrom" = "already" ] || [ "$cardbootrom" = "set" ]; then
        echo done
    else
        echo problem
    fi
}

function uefipass()
{
    local mlxdev=$1
    local uefival
    # Livefish cannot write config at all, and a set on a still-locked image
    # is lost when the card is unlocked. Either way, not in this pass.
    if [ "$cardlivefish" = "yes" ]; then
        echo "$mlxdev: jumper fitted (livefish) -- the boot ROM settings cannot be set" \
             "now. Remove the jumper and run again."
        cardbootrom=pending
        return 0
    fi
    if [ "$devsecure" = "secure-fw" ]; then
        echo "$mlxdev: still locked -- boot ROM settings left for the pass after the unlock."
        cardbootrom=pending
        return 0
    fi
    romneeds "$(domstconfig query "$mlxdev" 2>/dev/null)"
    # A card whose new image is in flash but not running reports the OUTGOING
    # image's config, so "already on" is not evidence about the image the card
    # will actually boot. Set everything regardless and let the next pass confirm.
    if [ -z "$romargs" ] && [ "$cardactivated" != "no" ]; then
        echo "$mlxdev: boot ROM config already set (UEFI x86 ROM, PXE ROM, legacy PXE)"
        cardbootrom=already
        return 0
    fi
    if [ "$cardactivated" == "no" ]; then
        echo "$mlxdev: card not activated; setting every boot ROM key regardless"
        romneeds ""
    else
        echo "$mlxdev: boot ROM config needs: $romtags"
    fi
    local want="$romargs" tags="$romtags"
    progress "setting the boot ROM ($tags) on card $devmac -- DO NOT PULL."
    if ! domstconfig set "$mlxdev" "$want"; then
        echo "$mlxdev: WARNING mstconfig set FAILED ($tags); the card will not PXE boot."
        cardbootrom=failed
        return 0
    fi
    # Read it back: the set writes the Next Boot column, so that is the
    # evidence the card will boot with the ROMs on.
    romneeds "$(domstconfig query "$mlxdev" 2>/dev/null)"
    if [ -n "$romargs" ]; then
        echo "$mlxdev: WARNING set reported OK but still wrong on Next Boot: $romtags"
        cardbootrom=failed
        return 0
    fi
    cardromset="$tags"
    cardbootrom=set
    nicchanged=1
    domstfwreset "$mlxdev"
    needbmcreset=1
    sleep 5
}

anyproblem=0
anyflip=0
flipwhy=""
cardblocks=""
cardjson=""
for mlxdev in $targets; do
    echo "--- $mlxdev ---"
    if ! getdevinfo "$mlxdev"; then
        echo "$mlxdev: RESULT query failed; verdict=problem"
        anyproblem=1
        continue
    fi
    narrowpsid
    cardident
    cardopn "$mlxdev"
    cardmode "$mlxdev"
    # Per-card state the two passes report back through.
    cardnoimage=no
    cardburned=no
    cardactivated=n/a
    cardresetrc=n/a
    cardbootaddr=n/a
    cardbootrom=unknown
    cardromset=none
    echo "$mlxdev: card mac=$devmac guid=$devguid opn=$devopn psid=$devpsid fw=$devfwver sec=[$devsecure] livefish=$cardlivefish"

    burnpass "$mlxdev"
    uefipass "$mlxdev"
    cardv=$(cardverdict)
    cardjson="$cardjson${cardjson:+,}$(cardjsonobj "$cardv")"
    # Finished: freeze this card's block so a second card does not hide it.
    cardblocks="$cardblocks$(cardfields "$cardv")
"
    case "$cardv" in
        problem) anyproblem=1 ;;
        remove-jumper|fit-jumper) anyflip=1; flipwhy="$flipwhy $cardv" ;;
    esac

    # ONE greppable line per card per run -- this is the station's history.
    # `grep RESULT /var/log/flax/mezz-flash/*.log` answers "was this card ever
    # unlocked, and did it get its boot ROM settings" without reading any prose.
    echo "$mlxdev: RESULT mac=$devmac guid=$devguid opn=$devopn psid=$devpsid fw=$devfwver" \
         "sec=[$devsecure] burned=$cardburned bootaddr=$cardbootaddr" \
         "activated=$cardactivated resetrc=$cardresetrc bootrom=$cardbootrom" \
         "romset=$cardromset livefish=$cardlivefish verdict=$cardv"
    if [ "$cardactivated" == "no" ]; then
        echo "$mlxdev: NEEDS-SECOND-PASS mac=$devmac -- cold power cycle, then re-run"
    fi
done

# --- signal ---------------------------------------------------------------
# Ordering matters: a BMC cold reset clears identify state, so it must happen
# BEFORE the blink is set, and the BMC must be answering again first. Reversed,
# the blade sits dark and finished-looking-like-still-working.
if [ $needbmcreset -ne 0 ]; then
    echo "cold-resetting BMC after boot ROM change"
    progress "resetting the BMC (about 1 min) -- DO NOT PULL."
    ipmitool mc reset cold
    for _ in $(seq 1 30); do
        sleep 5
        if ipmitool mc info >/dev/null 2>&1; then echo "BMC back"; break; fi
    done
fi

# Keep "insert the blade and it boots" true across BMC resets.
ipmitool chassis policy always-on || true

# --- verdict --------------------------------------------------------------
# The operator sees at most power state + IDENT, and SOL only when the node is
# up WITHOUT the jumper (NC-SI rides the card, so a jumpered card leaves the
# BMC unreachable). Three outcomes, operator decision 2026-09-22:
#
#   DONE     IDENT blinking. Powered off only if this run changed a card.
#   FLIP     IDENT off, powered off: flip the jumper state and run again.
#   PROBLEM  IDENT off, left up: something failed; SOL / the log says what.
if [ -z "$targets" ] || [ "$anyproblem" -ne 0 ]; then
    verdict=PROBLEM
elif [ "$anyflip" -ne 0 ]; then
    verdict=FLIP
else
    verdict=DONE
fi

# `<state>  <text>`: the state is a fixed token so a triage agent can parse
# the status line without reading prose (operator intent 2026-09-22). The
# operator wording lives in the text.
case "$verdict:$flipwhy" in
    DONE:*)             state=DONE
                        headline="card unlocked, boot ROM on (UEFI+PXE). Pull it." ;;
    FLIP:*remove-jumper*fit-jumper*|FLIP:*fit-jumper*remove-jumper*)
                        state=MIXED
                        headline="cards need different jumper states; see below." ;;
    FLIP:*remove-jumper*) state=REMOVE-JUMPER
                        headline="half done: REMOVE THE JUMPER and run again (boot ROM still to set)." ;;
    FLIP:*fit-jumper*)  state=FIT-JUMPER
                        headline="still locked: FIT THE JUMPER and run again (cannot unlock without it)." ;;
    PROBLEM:*)          state=PROBLEM
                        headline="not finished; read the rows below." ;;
esac
if [ -z "$targets" ]; then
    state=PROBLEM
    headline="no Mellanox card found."
fi
writestatus "$state" "$headline" "$cardjson"

# The banner: printed on the console now, and left in the serial getty's issue
# file so pressing Enter in SOL shows it again later. It carries recent RESULT
# lines from EARLIER runs too: a jumpered run's verdict can only be read on a
# later jumper-less boot.
banner=$(
    # The card fields are already frozen into $cardblocks by the loop, so pass
    # an empty live block here.
    devmac=""
    bannerbody "$(date -u +%FT%TZ)  $state  $headline"
)
echo "$banner"
timeout 5 sh -c 'printf "\r\n%s\r\n" "$1" | sed "s/$/\r/" > "$2"' _ "$banner" "$soldev" 2>/dev/null || true
mkdir -p "$(dirname "$issuefile")" 2>/dev/null
printf "%s\n%s\n\n" "$banner" "$clockline" > "$issuefile" 2>/dev/null || true
agetty --reload >/dev/null 2>&1 || true

if [ "$verdict" = "DONE" ]; then
    echo "lighting identify (indefinite blink) = DONE"
    ipmitool chassis identify force
else
    ipmitool chassis identify 0 || true
fi
echo "=== end $(date -u +%FT%TZ) verdict=$verdict ==="

# Identify first, then power off -- the same order post.sh uses, where it is
# known to survive the transition (the BMC runs on standby power, so the blink
# outlives the host). Reversed, the blade goes dark before the LED is set and
# the operator has no signal at all.
#
# NOT `ipmitool chassis power off`, which post.sh uses: that is an immediate
# hard off, harmless there because post runs from a RAM-based live ISO with no
# disk to dirty. This station has a real rootfs that is otherwise hard-cut on
# every blade pull, and avoiding that is the whole point of powering down.
#
# DONE powers off only if a card was changed: a no-op run stays up, so a node
# carrying a finished card can still be booted to investigate things.
# FLIP always powers off -- dark is its whole signal. PROBLEM stays up, so it
# can be read over SOL / ssh.
poweroff=0
case "$verdict" in
    DONE) [ "$nicchanged" -ne 0 ] && poweroff=1 ;;
    FLIP) poweroff=1 ;;
esac
if [ "$poweroff" -eq 0 ]; then
    [ "$verdict" = "DONE" ] && echo "NO-CHANGE -- no card was burned or reconfigured; staying up"
    [ "$verdict" = "PROBLEM" ] && echo "PROBLEM -- staying up so it can be read"
elif [ "${SHUTDOWN_ON_DONE:-1}" = "1" ]; then
    echo "powering off cleanly (rootfs stays consistent across the pull)"
    sync
    shutdown -h now
else
    echo "SHUTDOWN_ON_DONE=0 -- staying up (dev: log readable over ssh)"
fi
# Explicit: the verdict is on the rack and in the log, not in the exit status.
# Without this the last `[ ... ] && echo` above leaks a 1 and the unit reads
# "failed" after a perfectly good run.
exit 0
