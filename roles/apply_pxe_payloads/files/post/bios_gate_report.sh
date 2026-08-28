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

# Overall wall-clock bound on the whole ssh round trip. -o ConnectTimeout
# bounds the TCP connect and NOTHING else: server-side, bios_gate's
# resolve_port walks every switch port with a 10s GET each until the MAC
# matches, so a degraded port API would otherwise block post.sh for minutes on
# every TiogaPass inventory boot. No answer is a safe answer (not-ACK).
ssh_timeout="${FLAX_GATE_SSH_TIMEOUT:-60}"

# On ACK this script must NOT return to post.sh. shutdown(8) is asynchronous:
# it schedules the halt and returns immediately, so returning here drops
# straight into post.sh's ./update_mellanox.sh and starts an mstflint/flint
# burn on a machine that is powering down -- systemd kills it mid-write and
# the NIC is bricked. So we block here until the kernel takes the machine
# away. Bounded (and overridable to ~0 in tests) so a shutdown that never
# lands cannot hang an inventory boot for ever.
shutdown_wait="${FLAX_SHUTDOWN_WAIT:-600}"

mac="${FLAX_BOOTIF_MAC:-}"
if [ -z "$mac" ]; then
    # FLAX_CMDLINE_FILE lets tests point this at a stub file; production
    # always defaults to the real /proc/cmdline.
    cmdline_file="${FLAX_CMDLINE_FILE:-/proc/cmdline}"
    # -n + the trailing p is load-bearing: without it, sed prints every
    # input line whether or not BOOTIF= matched, so a cmdline with no
    # BOOTIF stanza would silently become the WHOLE cmdline (dashes
    # stripped) instead of empty, defeating the skip path below.
    mac=$(sed -nre 's/^.*BOOTIF=01-([^ ]+).*$/\1/p' "$cmdline_file" 2>/dev/null | tr -d '-')
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

# ssh must not read post.sh's stdin -- it would swallow input the rest of the
# boot script expects. `-n` is the ssh-native way to say that; the explicit
# `< /dev/null` is the same guarantee at the shell level, and it keeps
# holding if this command ever grows a wrapper that does not forward the
# flag. Belt and braces on a machine nobody can walk over to.
remote_cmd="bios_gate report --mac $(_sq "$mac") --current $(_sq "$biosver")"
answer=$(timeout "$ssh_timeout" ssh -n -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=no "$dst" "$remote_cmd" \
    < /dev/null 2>/dev/null) || answer=""

verb=$(echo "$answer" | head -n1 | awk '{print $1}')
echo "bios_gate_report: answer=${answer:-<none>}"

if [ "$verb" = "ACK" ]; then
    echo "bios_gate_report: authorised -- powering off for BIOS update"
    shutdown -h now
    # Do not come back. See $shutdown_wait above: the spec is "skip inventory
    # and shut down", and post.sh runs update_mellanox.sh the instant this
    # returns.
    echo "bios_gate_report: waiting ${shutdown_wait}s for shutdown to take effect"
    sleep "$shutdown_wait"
    echo "bios_gate_report: still up after ${shutdown_wait}s -- shutdown did not take"
fi

exit 0
