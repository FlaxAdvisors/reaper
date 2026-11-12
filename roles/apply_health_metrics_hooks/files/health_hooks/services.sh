#!/bin/bash
# What runs on this bang, for the site health board (plan 2026-11-09-site-health-board):
#   reaper_container_running{name}    1 running, 0 any other state (docker ps -a)
#   reaper_container_restarts{name}   docker's RestartCount
#   reaper_listening{proto,port}      1 per TCP/UDP port something listens on (ss -ltnu)
# Expected units/containers/ports come from reaper_expected (expected.prom, written at deploy).
set -u
umask 0022
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}

TMP="$TEXTFILE_DIR/services.prom.$$"
{
  echo "# TYPE reaper_container_running gauge"
  echo "# TYPE reaper_container_restarts gauge"
  docker ps -a --format '{{.Names}} {{.State}}' 2>/dev/null | while read -r name state; do
    [ -n "$name" ] || continue
    echo "reaper_container_running{name=\"$name\"} $([ "$state" = running ] && echo 1 || echo 0)"
    r=$(docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null | tr -dc '0-9')
    echo "reaper_container_restarts{name=\"$name\"} ${r:-0}"
  done
  echo "# TYPE reaper_listening gauge"
  ss -ltnuH 2>/dev/null | awk '{ a=$5; sub(/.*:/, "", a); if (a ~ /^[0-9]+$/) print $1, a }' | sort -u |
    awk '{ printf "reaper_listening{proto=\"%s\",port=\"%s\"} 1\n", $1, $2 }'
} > "$TMP"
mv "$TMP" "$TEXTFILE_DIR/services.prom"
