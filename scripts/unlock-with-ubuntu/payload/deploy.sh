#!/bin/bash
# deploy.sh -- install the unlock station onto a fresh Ubuntu node.
#
#   scp unlock-with-ubuntu.tgz node:.
#   ssh node 'mkdir -p unlock && tar -C unlock -xf unlock-with-ubuntu.tgz \
#             && cd unlock && sudo ./deploy.sh'
#
# Run this while the node still HAS a working NIC: it is the only step that
# needs the network (apt). Afterwards the station runs with no link at all.
# Idempotent -- re-run it to upgrade a station in place.
#
# --update: the in-boot path, run by self_update.sh from a verified staging
# tree (mezz-flash-update.service, before every flash pass). No apt -- there
# is no mirror to reach from inside the boot path, so missing packages refuse
# the update instead -- and nothing is started: mezz-flash-serial.service is
# After=mezz-flash.service, which is After= the updater, so a blocking start
# from here would deadlock the boot until the unit timeout.
#
# MEZZ_ROOT prefixes every path this writes (tests only; empty on a station).
set -eu

update=0
[ "${1:-}" = "--update" ] && update=1

[ "$(id -u)" = "0" ] || { echo "deploy.sh: must run as root"; exit 1; }

here=$(cd "$(dirname "$0")" && pwd)
root="${MEZZ_ROOT:-}"
dst="$root/opt/flax/mezzflash"
sysd="$root/etc/systemd/system"
# Keep in step with the apt-get install line below.
pkgs="mstflint ipmitool setserial curl"

if [ "$update" = 1 ]; then
    echo "== packages (update: must already be installed) =="
    # shellcheck disable=SC2086
    dpkg -s $pkgs >/dev/null 2>&1 \
        || { echo "deploy.sh --update: missing one of: $pkgs -- reinstall with network"; exit 1; }
else
    echo "== packages (needs network; the deployed station will have none) =="
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq mstflint ipmitool setserial curl    # == $pkgs
fi

echo "== in-band IPMI =="
# The BMC is unreachable over LAN once a jumpered card is seated, so every
# ipmitool call goes over KCS. Load the modules at boot rather than hoping.
mkdir -p "$root/etc/modules-load.d"
cat > "$root/etc/modules-load.d/flax-ipmi.conf" <<'EOF'
ipmi_si
ipmi_devintf
EOF
modprobe ipmi_si 2>/dev/null || true
modprobe ipmi_devintf 2>/dev/null || true

echo "== station identity =="
# A station is keyed on its blade FRU serial, so a blade that cannot report
# one cannot be tracked -- and an untracked station silently flashes cards
# nobody can audit. Operator decision 2026-09-22: fault the slot, build
# another. Gate it HERE, at commissioning, where a human is standing by and
# the network is up, rather than at 3am on a rack.
ident_err=$(mktemp)
trap 'rm -f "$ident_err"' EXIT
# Under `set -eu`, a plain `x=$(cmd)` takes cmd's exit status -- -e would
# fire right here and kill the script before ident_rc=$? ever ran, silently
# skipping the FATAL block below (and everything after it). The `|| ident_rc=$?`
# form lets a failing station_ident.py be handled instead of fatal to the
# whole script.
ident_rc=0
ident_json=$("$here/station_ident.py" 2>"$ident_err") || ident_rc=$?
if [ "$ident_rc" != "0" ]; then
    echo "$ident_json"
    cat "$ident_err" >&2
    echo >&2
    echo "FATAL: this blade cannot be a flash station." >&2
    echo "       Fault the slot and build another flash server." >&2
    exit 1
fi
rm -f "$ident_err"
echo "   $(echo "$ident_json" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("%s (%s, family %s)" % (d["station_sn"], d["serial_field"], d["family"]))')"

echo "== config =="
mkdir -p "$root/etc/flax"
if [ ! -f "$root/etc/flax/mezz-flash.conf" ]; then
    cat > "$root/etc/flax/mezz-flash.conf" <<'EOF'
# Cards the station must never flash, as bus:device or a full BDF, space
# separated. Cards holding an IPv4 address are skipped automatically, but only
# once DHCP has run -- and the unit does not wait for the network, because a
# deployed station has none. Pin anything that matters here rather than relying
# on that. A deployed blade has only the mezzanine card, so this is normally
# empty; it exists for development nodes carrying a PCIe uplink.
PROTECT_PCI=""

# Power the node off when the run completes, after lighting IDENT. The blade is
# pulled live on every cycle, so without this the rootfs is hard-cut every time.
# Set to 0 on a development node to keep it up and sshable for reading logs.
# A run that changed no card never powers off, whatever this says.
SHUTDOWN_ON_DONE="1"

# Where to push status for the triage tile. Empty = derive from the default
# route (the bang is the gateway on every lab VLAN). Set explicitly only if
# this station is on a network whose gateway is not the bang.
MEZZ_PUSH_URL=""
EOF
fi

echo "== filesystem: the blade is pulled live on every cycle =="
# Never a clean shutdown, so a periodic fsck would eventually fire mid-cycle
# and stall a boot the operator is standing there waiting on.
root_dev=$(findmnt -no SOURCE / || true)
if [ -n "$root_dev" ]; then
    tune2fs -c 0 -i 0 "$root_dev" 2>/dev/null \
        || echo "  (tune2fs skipped: $root_dev is not ext-family)"
fi
if [ -f "$root/etc/default/grub" ] && ! grep -q "fsck.mode=skip" "$root/etc/default/grub"; then
    sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"$/GRUB_CMDLINE_LINUX_DEFAULT="\1 fsck.mode=skip"/' \
        "$root/etc/default/grub"
    update-grub 2>/dev/null || true
fi

echo "== power policy: inserted blade must boot on its own =="
ipmitool chassis policy always-on || echo "  (could not set power policy)"

echo "== payload -> $dst =="
# Swapped in by rename, never copied over the live tree: nothing stale from
# the old bundle survives, the previous tree stays at $dst.prev for a manual
# rollback (mv it back), and a failure anywhere above leaves the running
# station untouched -- which is what lets self_update.sh call this at boot.
rm -rf "$dst.new"
mkdir -p "$dst.new"
cp -a "$here"/. "$dst.new"/
rm -f "$dst.new/deploy.sh"
chmod 0755 "$dst.new/unlock_mellanox.sh" "$dst.new/common_mellanox.sh" "$dst.new/mezz_select.py" \
    "$dst.new/serial_console_fixup.sh" "$dst.new/serial_watchdog.sh" "$dst.new/station_ident.py" \
    "$dst.new/self_update.sh"
rm -rf "$dst.prev"
[ -d "$dst" ] && mv "$dst" "$dst.prev"
mv "$dst.new" "$dst"

echo "== service =="
mkdir -p "$sysd"
for u in mezz-flash.service mezz-flash-update.service mezz-flash-banner.service \
         mezz-flash-serial.service mezz-flash-watchdog.service \
         mezz-flash-watchdog.timer mezz-flash-banner.timer; do
    install -m 0644 "$here/$u" "$sysd/$u"
done
systemctl daemon-reload
# mezz-flash-update.service needs no enable: mezz-flash.service Wants= it, so
# it runs before every flash pass from the next boot on.
if [ "$update" = 1 ]; then
    # Already enabled by the first install; enable is idempotent and links any
    # unit this bundle added. Never --now from inside the boot path (header).
    systemctl enable mezz-flash-banner.timer mezz-flash-watchdog.timer \
        mezz-flash-serial.service 2>/dev/null || true
    systemctl enable mezz-flash.service 2>/dev/null || true
else
    # The banner timer is safe to start now: it only redraws the login prompt.
    systemctl enable --now mezz-flash-banner.timer
    # Safe to start now: it only restarts the SOL login prompt, and only when that
    # port has stopped transmitting (at most once per 5 min).
    systemctl enable --now mezz-flash-watchdog.timer
    # Enabled, but ordered after mezz-flash.service so it cannot hang a boot: the
    # early version cost et9b1 a 22-minute boot (TX-stalled port + console=ttyS1 =
    # ~30s per PID1 console line). It also reverts the port to `uart none` if the
    # re-probe does not restore TX.
    systemctl enable --now mezz-flash-serial.service 2>/dev/null \
        || echo "  (mezz-flash-serial enable refused -- masked this boot?)"

    # Debian's own setserial services save the port state at shutdown and restore
    # it at boot -- the other half of that race. Never let them run, and drop the
    # state they saved so nothing restores it.
    systemctl disable --now setserial.service etc-setserial.service 2>/dev/null || true
    systemctl mask setserial.service etc-setserial.service 2>/dev/null || true
    rm -f "$root"/var/lib/setserial/autoserial.conf "$root"/var/lib/setserial/autoserial.conf.old
    # `|| true`: a boot with systemd.mask=mezz-flash.service on the kernel line
    # (how a station is booted to be upgraded) makes enable fail, which under
    # set -e used to abort the install before this point. The enable link from
    # the first install survives that mask, and the mask itself is /run-only.
    systemctl enable mezz-flash.service || echo "  (enable refused -- runtime-masked this boot? link kept from the first install)"
    # Deliberately NOT started here: installing should never flash a card that the
    # operator has not yet jumpered and seated on purpose.
fi

echo
echo "Installed. It runs on the NEXT boot."
# Type=simple: once a run has exited the unit is inactive, so `start` runs it
# again. It does NOT hold boot; watch progress on the SOL banner.
echo "  re-run now  : sudo systemctl start mezz-flash"
echo "  logs        : /var/log/flax/mezz-flash/"
echo "  config      : /etc/flax/mezz-flash.conf (PROTECT_PCI, SHUTDOWN_ON_DONE)"
