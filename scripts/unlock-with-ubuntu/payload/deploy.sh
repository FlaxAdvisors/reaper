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
set -eu

[ "$(id -u)" = "0" ] || { echo "deploy.sh: must run as root"; exit 1; }

here=$(cd "$(dirname "$0")" && pwd)
dst=/opt/flax/mezzflash

echo "== packages (needs network; the deployed station will have none) =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq mstflint ipmitool setserial

echo "== payload -> $dst =="
mkdir -p "$dst"
cp -a "$here"/. "$dst"/
rm -f "$dst/deploy.sh"
chmod 0755 "$dst/unlock_mellanox.sh" "$dst/common_mellanox.sh" "$dst/mezz_select.py" \
    "$dst/serial_console_fixup.sh" "$dst/serial_watchdog.sh"

echo "== in-band IPMI =="
# The BMC is unreachable over LAN once a jumpered card is seated, so every
# ipmitool call goes over KCS. Load the modules at boot rather than hoping.
cat > /etc/modules-load.d/flax-ipmi.conf <<'EOF'
ipmi_si
ipmi_devintf
EOF
modprobe ipmi_si 2>/dev/null || true
modprobe ipmi_devintf 2>/dev/null || true

echo "== config =="
mkdir -p /etc/flax
if [ ! -f /etc/flax/mezz-flash.conf ]; then
    cat > /etc/flax/mezz-flash.conf <<'EOF'
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
if [ -f /etc/default/grub ] && ! grep -q "fsck.mode=skip" /etc/default/grub; then
    sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"$/GRUB_CMDLINE_LINUX_DEFAULT="\1 fsck.mode=skip"/' \
        /etc/default/grub
    update-grub 2>/dev/null || true
fi

echo "== power policy: inserted blade must boot on its own =="
ipmitool chassis policy always-on || echo "  (could not set power policy)"

echo "== service =="
install -m 0644 "$here/mezz-flash.service" /etc/systemd/system/mezz-flash.service
install -m 0644 "$here/mezz-flash-banner.service" /etc/systemd/system/mezz-flash-banner.service
install -m 0644 "$here/mezz-flash-serial.service" /etc/systemd/system/mezz-flash-serial.service
install -m 0644 "$here/mezz-flash-watchdog.service" /etc/systemd/system/mezz-flash-watchdog.service
install -m 0644 "$here/mezz-flash-watchdog.timer" /etc/systemd/system/mezz-flash-watchdog.timer
install -m 0644 "$here/mezz-flash-banner.timer" /etc/systemd/system/mezz-flash-banner.timer
systemctl daemon-reload
# The banner timer is safe to start now: it only redraws the login prompt.
systemctl enable --now mezz-flash-banner.timer
# Safe to start now: it only restarts the SOL login prompt, and only when that
# port has stopped transmitting (at most once per 5 min).
systemctl enable --now mezz-flash-watchdog.timer
# Installed but NOT enabled (2026-09-22). On et9b1 re-probing the SOL UART at
# boot brought it up TX-stalled, and with console=ttyS1 on the kernel line every
# PID1 status line then blocked ~30s: ssh came up 22 minutes into boot. It stays
# off until console= is off the station's kernel line.
systemctl disable mezz-flash-serial.service 2>/dev/null || true

# Debian's own setserial services save the port state at shutdown and restore
# it at boot -- the other half of that race. Never let them run, and drop the
# state they saved so nothing restores it.
systemctl disable --now setserial.service etc-setserial.service 2>/dev/null || true
systemctl mask setserial.service etc-setserial.service 2>/dev/null || true
rm -f /var/lib/setserial/autoserial.conf /var/lib/setserial/autoserial.conf.old
# `|| true`: a boot with systemd.mask=mezz-flash.service on the kernel line
# (how a station is booted to be upgraded) makes enable fail, which under
# set -e used to abort the install before this point. The enable link from
# the first install survives that mask, and the mask itself is /run-only.
systemctl enable mezz-flash.service || echo "  (enable refused -- runtime-masked this boot? link kept from the first install)"
# Deliberately NOT started here: installing should never flash a card that the
# operator has not yet jumpered and seated on purpose.

echo
echo "Installed. It runs on the NEXT boot."
# Type=simple: once a run has exited the unit is inactive, so `start` runs it
# again. It does NOT hold boot; watch progress on the SOL banner.
echo "  re-run now  : sudo systemctl start mezz-flash"
echo "  logs        : /var/log/flax/mezz-flash/"
echo "  config      : /etc/flax/mezz-flash.conf (PROTECT_PCI, SHUTDOWN_ON_DONE)"
