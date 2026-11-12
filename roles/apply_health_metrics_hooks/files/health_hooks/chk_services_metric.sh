#!/bin/bash
# Runs /usr/local/bin/check_lab_services.sh and exposes its exit code +
# the derived effective VRRP priority as Prometheus metrics.

set -u
umask 0022
TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector
HOST=$(hostname -s)

# Base priority from keepalived.conf (e.g. fiesta=110, siesta=90).
BASE_PRIO=$(awk '/^[[:space:]]*priority[[:space:]]+[0-9]+/ {print $2; exit}' /etc/keepalived/keepalived.conf 2>/dev/null || echo 0)
WEIGHT_PENALTY=30

/usr/local/bin/check_lab_services.sh >/dev/null 2>&1
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
  EFFECTIVE=$BASE_PRIO
else
  EFFECTIVE=$((BASE_PRIO - WEIGHT_PENALTY))
fi

TMP="$TEXTFILE_DIR/chk_services.prom.$$"
{
  echo "# HELP chk_services_exit_code Exit code of /usr/local/bin/check_lab_services.sh"
  echo "# TYPE chk_services_exit_code gauge"
  echo "chk_services_exit_code{bang=\"$HOST\"} $EXIT_CODE"
  echo "# HELP effective_vrrp_priority Configured priority minus chk_services weight penalty"
  echo "# TYPE effective_vrrp_priority gauge"
  echo "effective_vrrp_priority{bang=\"$HOST\"} $EFFECTIVE"
} > "$TMP"
mv "$TMP" "$TEXTFILE_DIR/chk_services.prom"
