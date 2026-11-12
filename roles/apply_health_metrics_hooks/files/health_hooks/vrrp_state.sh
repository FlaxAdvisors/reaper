#!/bin/bash
# keepalived's /usr/local/sbin/notify_{master,backup,fault}.sh call this with the new
# state as $1, on a transition. health-hook@vrrp_state.timer calls it with no argument
# every 30 s and it reads the state off the host, so the file is never stale and a
# stopped keepalived shows (the notify scripts alone left it frozen for weeks):
#   MGMT_VIP (/etc/flax/site.env) on an interface -> MASTER (1)
#   keepalived active without it                   -> BACKUP (0)
#   keepalived not running                         -> STOPPED (2, like FAULT)
# Writes vrrp_state.prom atomically into the node_exporter textfile collector.

set -u
umask 0022
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}
SITE_ENV=${SITE_ENV:-/etc/flax/site.env}
HOST=$(hostname -s)
STATE="${1:-}"

if [ -z "$STATE" ]; then
  vip=$(sed -n 's/^MGMT_VIP=//p' "$SITE_ENV" 2>/dev/null | tr -d '"' | head -1)
  if [ -n "$vip" ] && ip -o -4 addr show | grep -qF " $vip/"; then
    STATE=MASTER
  elif systemctl is-active --quiet keepalived; then
    STATE=BACKUP
  else
    STATE=STOPPED
  fi
fi

TMP="$TEXTFILE_DIR/vrrp_state.prom.$$"
{
  echo "# HELP vrrp_state Current keepalived VRRP state (0=BACKUP, 1=MASTER, 2=FAULT or keepalived stopped)"
  echo "# TYPE vrrp_state gauge"
  case "$STATE" in
    MASTER) val=1 ;;
    BACKUP) val=0 ;;
    *)      val=2 ;;
  esac
  echo "vrrp_state{instance=\"$HOST\",state=\"$STATE\"} $val"
} > "$TMP"
mv "$TMP" "$TEXTFILE_DIR/vrrp_state.prom"
