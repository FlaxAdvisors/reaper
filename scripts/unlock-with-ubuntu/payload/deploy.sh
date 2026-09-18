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
apt-get install -y -qq mstflint ipmitool

echo "== payload -> $dst =="
mkdir -p "$dst"
cp -a "$here"/. "$dst"/
rm -f "$dst/deploy.sh"
chmod 0755 "$dst/unlock_mellanox.sh" "$dst/common_mellanox.sh" "$dst/mezz_select.py"

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
# separated. Cards holding an IPv4 address are skipped automatically; this is
# for one that is unaddressed at boot but still must not be touched.
PROTECT_PCI=""
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
systemctl daemon-reload
systemctl enable mezz-flash.service
# Deliberately NOT started here: installing should never flash a card that the
# operator has not yet jumpered and seated on purpose.

echo
echo "Installed. It runs on the NEXT boot."
echo "  dry run now : sudo systemctl start mezz-flash && journalctl -u mezz-flash -f"
echo "  logs        : /var/log/flax/mezz-flash/"
