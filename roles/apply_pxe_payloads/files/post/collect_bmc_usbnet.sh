#!/bin/bash
# collect_bmc_usbnet.sh -- the BMC's USB ethernet (in-band host<->BMC link).
#
# Our OpenBMC image presents a USB RNDIS gadget to the host (046b:ffb0
# "Virtual Ethernet": the tree is AMI-derived, hence the AMI USB id; AMI's own
# MegaRAC firmware does not). The live ISO binds no driver to it on its own, so
# the link was never exercised. This loads the driver, finds the netdev the
# kernel hangs under that USB interface (never by name: it takes whatever ethN
# is free), brings it up on IPv6 link-local only, and pings the BMC.
#
# Verified on et8b1 2026-09-29: host eth1 02:00:00:aa:bb:02 fe80::ff:feaa:bb02,
# BMC answers ff02::1 as fe80::ff:feaa:bb01 (02:00:00:aa:bb:01). Both are fixed
# on every blade, which is fine on a point-to-point link; the BMC address is
# still discovered from the all-nodes replies and only compared to that value.
#
# Prints "key: value" lines, then the raw command output. Always exits 0 and
# stays well under a minute: a missing or dead link is a finding, not an error.
# Run it after the NIC tools (lsnet, collect_mellanox): the new netdev takes an
# ethN name. It is left up on purpose -- a staylive node keeps it for debug.
#
# Test seams: BMC_USBNET_SYSFS (default /sys), BMC_USBNET_WAIT_S (default 10).

SYS="${BMC_USBNET_SYSFS:-/sys}"
WAIT="${BMC_USBNET_WAIT_S:-10}"
EXPECTED_BMC_LL="fe80::ff:feaa:bb01"
RAW=""

kv() { echo "$1: $2"; }
raw() { RAW+="\$ $1"$'\n'"$2"$'\n'; }
finish() { kv verdict "$1"; echo "----- raw"; printf '%s' "$RAW"; exit 0; }

# USB interface class triplets: RNDIS (what our BMC presents), CDC-ECM, CDC-NCM.
intf="" kind=""
for i in "$SYS"/bus/usb/devices/*:*; do
    [ -r "$i/bInterfaceClass" ] || continue
    c=$(cat "$i/bInterfaceClass") s=$(cat "$i/bInterfaceSubClass" 2>/dev/null) p=$(cat "$i/bInterfaceProtocol" 2>/dev/null)
    case "$c/$s/$p" in
        02/02/ff) kind=rndis ;;
        02/06/*)  kind=ecm ;;
        02/0d/*)  kind=ncm ;;
        *) continue ;;
    esac
    intf="$i"; break
done
if [ -z "$intf" ]; then
    kv present no
    finish absent
fi

dev="${intf%:*}"
kv present yes
kv kind "$kind"
kv usb "$(basename "$intf")"
kv vendor_product "$(cat "$dev/idVendor" 2>/dev/null):$(cat "$dev/idProduct" 2>/dev/null)"
kv product "$(cat "$dev/product" 2>/dev/null)"

case "$kind" in
    rndis) mod=rndis_host ;;
    ecm)   mod=cdc_ether ;;
    ncm)   mod=cdc_ncm ;;
esac
out=$(modprobe "$mod" 2>&1); rc=$?
raw "modprobe $mod" "$out"
kv module "$mod rc=$rc"

iface=""
for _ in $(seq "$WAIT"); do
    for n in "$intf"/net/*; do
        [ -e "$n" ] && iface=$(basename "$n") && break 2
    done
    sleep 1
done
if [ -z "$iface" ]; then
    kv iface none
    finish no_iface
fi
kv iface "$iface"
kv iface_mac "$(cat "$intf/net/$iface/address" 2>/dev/null)"
kv driver "$(basename "$(readlink "$intf/driver" 2>/dev/null)" 2>/dev/null)"

sysctl -q -w "net.ipv6.conf.$iface.disable_ipv6=0" >/dev/null 2>&1
ip link set "$iface" up

host_ll=""
for _ in $(seq "$WAIT"); do
    out=$(ip -6 -o addr show dev "$iface" scope link 2>&1)
    if ! grep -q tentative <<<"$out"; then
        host_ll=$(grep -oE 'fe80::[0-9a-f:]+' <<<"$out" | head -n1)
        [ -n "$host_ll" ] && break
    fi
    sleep 1
done
raw "ip -6 -o addr show dev $iface scope link" "$out"
if [ -z "$host_ll" ]; then
    kv host_ll none
    finish no_ll
fi
kv host_ll "$host_ll"

PING6="ping6"
command -v ping6 >/dev/null 2>&1 || PING6="ping -6"
out=$($PING6 -c3 -w5 "ff02::1%$iface" 2>&1)
raw "$PING6 -c3 -w5 ff02::1%$iface" "$out"
bmc_ll=$(grep -oE 'from fe80::[0-9a-f:]+' <<<"$out" | awk '{print $2}' | grep -vx "$host_ll" | sort -u | head -n1)
if [ -z "$bmc_ll" ]; then
    kv bmc_ll none
    finish no_bmc
fi
kv bmc_ll "$bmc_ll"
kv bmc_ll_expected "$([ "$bmc_ll" = "$EXPECTED_BMC_LL" ] && echo yes || echo "no ($EXPECTED_BMC_LL)")"

out=$($PING6 -c3 -w5 "$bmc_ll%$iface" 2>&1); rc=$?
raw "$PING6 -c3 -w5 $bmc_ll%$iface" "$out"
kv ping_rc "$rc"
kv loss "$(grep -oE '[0-9.]+% packet loss' <<<"$out" | awk '{print $1}')"
kv rtt "$(grep -oE '= [0-9./]+ ms' <<<"$out" | sed 's/^= //')"
[ "$rc" -eq 0 ] && finish ok
finish bmc_unreachable
