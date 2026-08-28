#!/bin/bash
# Pre-inventory BIOS gate report (triage).
#
# Reports this node's in-band BIOS version to the bang and, ONLY on ACK, powers
# the node off so the biosfw agent can flash it while the host does not own the
# BIOS SPI bus. Everything else -- CLEAR, HOLD, NAK, no agent, no answer, an
# unreadable version -- returns 0 and lets post.sh carry on with its normal
# action. This script must never be the reason an inventory boot fails.
#
# The BMC cannot report a BIOS version (bios_active.Version is "null" on
# flax-onetree-1.1.0), so this in-band dmidecode read is the only source, and
# it is also how a completed update gets confirmed on the NEXT boot.
set -u

dst="${FLAX_BANG_SSH:-root@bang}"

mac="${FLAX_BOOTIF_MAC:-}"
if [ -z "$mac" ]; then
    mac=$(sed -re 's/^.*BOOTIF=01-([^ ]+).*$/\1/' /proc/cmdline 2>/dev/null | tr -d '-')
fi
if [ -z "$mac" ]; then
    echo "bios_gate_report: no BOOTIF mac; skipping"
    exit 0
fi

biosver=$(dmidecode -t bios 2>/dev/null | sed -nre 's/^[[:space:]]*Version:[[:space:]]*(\S+).*$/\1/p' | head -n1)
if [ -z "$biosver" ]; then
    echo "bios_gate_report: could not read BIOS version; skipping"
    exit 0
fi

echo "bios_gate_report: mac=$mac bios=$biosver"

# $mac and $biosver come from /proc/cmdline and dmidecode -- untrusted-ish
# input. The command below is a single argument to ssh, but sshd hands it to
# the remote user's shell to re-parse; single-quote each value so whitespace
# or a shell metacharacter in either one can't split into extra bios_gate
# arguments or inject a second command. Plain '\'' escaping is POSIX-shell
# portable (works whether the remote login shell is bash, dash, or sh).
_sq() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

remote_cmd="bios_gate report --mac $(_sq "$mac") --current $(_sq "$biosver")"
answer=$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$dst" \
    "$remote_cmd" 2>/dev/null) || answer=""

verb=$(echo "$answer" | head -n1 | awk '{print $1}')
echo "bios_gate_report: answer=${answer:-<none>}"

if [ "$verb" = "ACK" ]; then
    echo "bios_gate_report: authorised -- powering off for BIOS update"
    shutdown -h now
fi

exit 0
