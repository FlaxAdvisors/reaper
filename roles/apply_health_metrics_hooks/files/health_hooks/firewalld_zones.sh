#!/bin/bash
# Detects the SUSE ifcfg ZONE= drift pattern: emits one series per
# (zone, iface) so the dashboard shows zone membership at a glance.

set -u
umask 0022
TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector
HOST=$(hostname -s)

TMP="$TEXTFILE_DIR/firewalld_zones.prom.$$"
{
  echo "# HELP firewalld_zone_iface Interface membership in firewalld zones (1=member)"
  echo "# TYPE firewalld_zone_iface gauge"
  for zone in $(firewall-cmd --get-zones 2>/dev/null); do
    for iface in $(firewall-cmd --zone="$zone" --list-interfaces 2>/dev/null); do
      echo "firewalld_zone_iface{bang=\"$HOST\",zone=\"$zone\",iface=\"$iface\"} 1"
    done
  done
} > "$TMP"
mv "$TMP" "$TEXTFILE_DIR/firewalld_zones.prom"
