#!/bin/bash
# self_update.sh -- bring this station's bundle up to what the bang publishes.
#
# Runs as mezz-flash-update.service (oneshot) BEFORE every flash pass, so the
# pass always starts on whatever is installed when this exits -- nothing
# restarts itself mid-run. Before this existed, upgrading a station meant a
# boot with a known-good NIC, or a GRUB edit adding
# systemd.mask=mezz-flash.service, then scp + deploy.sh.
#
# The bang publishes, on every lab VLAN's gateway address:
#   /mezz-flash.version   version=<sha>[-dirty]@<build ts>, sha256=, size=
#   /mezz-flash.tgz       the bundle (25-60MB -- only pulled when the version differs)
#
# This must NEVER stop a flash run: flashing is the job, updating is not. Every
# path exits 0, and a failure anywhere leaves /opt/flax/mezzflash exactly as it
# was (deploy.sh swaps the new tree in as its last step).
#
# Safe to replace while running: deploy.sh swaps the install dir by rename,
# and bash keeps its fd on this file's old inode.

root="${MEZZ_ROOT:-}"
dst="$root/opt/flax/mezzflash"
stage="$root/opt/flax/mezzflash.staging"
issuefile="$root/etc/issue.d/mezz-flash-update.issue"
routewait="${MEZZ_ROUTE_WAIT:-20}"
netdevgrace="${MEZZ_NETDEV_GRACE:-3}"

localver=$(awk '/^version/ {print $3}' "$dst/MANIFEST" 2>/dev/null)
localver="${localver:-unknown}"

# One line above the station's own banner (issue.d is read in name order, and
# "mezz-flash-update" sorts before "mezz-flash."). A separate file, so the
# flash pass rewriting its banner at every step cannot wipe it.
function say()
{
    echo "self_update: $1"
    mkdir -p "$(dirname "$issuefile")" 2>/dev/null
    printf "%s  bundle: %s\n" "$(date -u +%FT%TZ)" "$1" > "$issuefile" 2>/dev/null || true
    agetty --reload >/dev/null 2>&1 || true
}

function finish()
{
    rm -rf "$stage" "$tmp" 2>/dev/null
    say "$1"
    exit 0
}

# Same as unlock_mellanox.sh's: the DEFAULT GATEWAY, not `bang` -- a station
# holding a free-pool lease may not route to the VIP that name resolves to,
# while the bang is the gateway on every lab VLAN. `via`-aware, so a
# scope-link route yields nothing instead of an interface name.
function _default_gw()
{
    ip -o route show default 2>/dev/null \
        | awk '{for(i=1;i<NF;i++) if($i=="via"){print $(i+1); exit}}'
}

tmp=$(mktemp -d) || finish "check failed (no tmp), running $localver"

# Any interface but lo, whatever its vendor: a dev node's PCIe uplink must
# still reach the bang while its jumpered mezz card is in. NOT an lspci check
# for Mellanox -- a livefish card still enumerates as vendor 15b3, it just
# never gets a netdev.
function _have_nic()
{
    local n
    for n in "$root"/sys/class/net/*; do
        [ -e "$n" ] || continue
        [ "${n##*/}" = lo ] || return 0
    done
    return 1
}

# The unit has no network dependency on purpose: a jumpered card never gets a
# lease. With only lo there is nothing to wait for, so give udev a short grace
# to create a late netdev (this unit does not wait for udev to settle) and
# move on -- the pathological jumpered boot costs ${netdevgrace}s, not the
# whole route wait.
for i in $(seq 1 "$netdevgrace"); do
    _have_nic && break
    [ "$i" -lt "$netdevgrace" ] && sleep 1
done
_have_nic || finish "no network (no NIC, only lo), running $localver"

gw=""
for _ in $(seq 1 "$routewait"); do
    gw=$(_default_gw)
    [ -n "$gw" ] && break
    sleep 1
done
[ -n "$gw" ] || finish "no network, running $localver"

base=""
for b in "http://$gw" "http://bang"; do
    if curl -fsS --connect-timeout 2 --max-time 5 \
            -o "$tmp/version" "$b/mezz-flash.version" 2>/dev/null; then
        base="$b"
        break
    fi
done
[ -n "$base" ] || finish "check failed (bang unreachable), running $localver"

pubver=$(sed -n 's/^version=//p' "$tmp/version" | head -1)
pubsha=$(sed -n 's/^sha256=//p' "$tmp/version" | head -1)
[ -n "$pubver" ] && [ -n "$pubsha" ] \
    || finish "check failed (bad version file), running $localver"

[ "$pubver" = "$localver" ] && finish "current $localver"

say "updating $localver -> $pubver ..."
curl -fsS --connect-timeout 2 --max-time 120 \
        -o "$tmp/mezz-flash.tgz" "$base/mezz-flash.tgz" 2>/dev/null \
    || finish "check failed (download), running $localver"

gotsha=$(sha256sum "$tmp/mezz-flash.tgz" | awk '{print $1}')
[ "$gotsha" = "$pubsha" ] \
    || finish "check failed (sha256 mismatch), running $localver"

rm -rf "$stage"
mkdir -p "$stage"
tar -C "$stage" -xzf "$tmp/mezz-flash.tgz" 2>/dev/null \
    || finish "check failed (bad tarball), running $localver"

# The version file and the tarball must describe the same build; otherwise
# the station reinstalls on every boot and never converges.
tgzver=$(awk '/^version/ {print $3}' "$stage/MANIFEST" 2>/dev/null)
[ "$tgzver" = "$pubver" ] \
    || finish "check failed (tarball is ${tgzver:-unversioned}, not $pubver), running $localver"

MEZZ_ROOT="$root" bash "$stage/deploy.sh" --update \
    || finish "install failed, running $localver"

finish "updated $localver -> $pubver"
