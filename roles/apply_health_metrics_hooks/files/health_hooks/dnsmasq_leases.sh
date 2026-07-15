#!/bin/bash
# Parses /var/lib/dnsmasq/dnsmasq.leases — fields are:
#   <epoch_expiry> <mac> <ip> <hostname> <client_id>

set -u
umask 0022
TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector
LEASES=/var/lib/dnsmasq/dnsmasq.leases
HOST=$(hostname -s)
NOW=$(date +%s)

TMP="$TEXTFILE_DIR/dnsmasq_leases.prom.$$"
{
  echo "# HELP dnsmasq_active_leases Count of active DHCP leases"
  echo "# TYPE dnsmasq_active_leases gauge"
  if [ -r "$LEASES" ]; then
    cnt=$(wc -l < "$LEASES")
    echo "dnsmasq_active_leases{bang=\"$HOST\"} $cnt"
    # Age distribution: count leases expiring in next 1h / 6h / 24h
    near=$(awk -v now="$NOW" '($1 > 0) && ($1 - now < 3600) {c++} END {print c+0}' "$LEASES")
    medium=$(awk -v now="$NOW" '($1 > 0) && ($1 - now < 21600) {c++} END {print c+0}' "$LEASES")
    long=$(awk -v now="$NOW" '($1 > 0) && ($1 - now < 86400) {c++} END {print c+0}' "$LEASES")
    echo "# HELP dnsmasq_lease_expiring_within Lease count expiring within a horizon"
    echo "# TYPE dnsmasq_lease_expiring_within gauge"
    echo "dnsmasq_lease_expiring_within{bang=\"$HOST\",horizon=\"1h\"} $near"
    echo "dnsmasq_lease_expiring_within{bang=\"$HOST\",horizon=\"6h\"} $medium"
    echo "dnsmasq_lease_expiring_within{bang=\"$HOST\",horizon=\"24h\"} $long"
  else
    echo "dnsmasq_active_leases{bang=\"$HOST\"} 0"
  fi
} > "$TMP"
mv "$TMP" "$TEXTFILE_DIR/dnsmasq_leases.prom"
