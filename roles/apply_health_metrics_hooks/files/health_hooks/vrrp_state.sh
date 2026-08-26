#!/bin/bash
# Called from /etc/keepalived/notify_master.sh / notify_backup.sh / notify_fault.sh
# with the new state as $1.
# Writes /var/lib/node_exporter/textfile_collector/vrrp_state.prom atomically.

set -u
umask 0022
TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector
HOST=$(hostname -s)
STATE="${1:-UNKNOWN}"

TMP="$TEXTFILE_DIR/vrrp_state.prom.$$"
{
  echo "# HELP vrrp_state Current keepalived VRRP state (0=BACKUP, 1=MASTER, 2=FAULT)"
  echo "# TYPE vrrp_state gauge"
  case "$STATE" in
    MASTER) val=1 ;;
    BACKUP) val=0 ;;
    FAULT)  val=2 ;;
    *)      val=2 ;;
  esac
  echo "vrrp_state{instance=\"$HOST\",state=\"$STATE\"} $val"
} > "$TMP"
mv "$TMP" "$TEXTFILE_DIR/vrrp_state.prom"
