#!/bin/bash
# Bang hardware health for the site health board (plan 2026-11-09-site-health-board).
# In-band IPMI over KCS (/dev/ipmi0, root): no network, no BMC credentials.
#   reaper_ipmi_kcs_ok                    1 when `ipmitool mc info` answers
#   reaper_ipmi_sensor_ok{sensor}         0 for a critical / non-recoverable SDR status
#                                         (cr nr lcr ucr lnr unr), else 1 (ok, ns, nc...)
#   reaper_sel_entries                    SEL entry count
#   reaper_sel_acked_entries              count someone acknowledged (SEL_ACK_FILE):
#                                         echo <count> | sudo tee /var/lib/reaper/sel-acked
#                                         absent: seeded with today's count (history is not new);
#                                         SEL cleared (entries < acked): reset to 0
#   reaper_dimm_ce/ue{slot,serial}        dimmerr (EDAC) per DIMM
set -u
umask 0022
TEXTFILE_DIR=${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}
SEL_ACK_FILE=${SEL_ACK_FILE:-/var/lib/reaper/sel-acked}
DIMMERR=${DIMMERR:-/opt/flax/bin/dimmerr}
esc() { sed 's/\\/\\\\/g; s/"/\\"/g'; }

TMP="$TEXTFILE_DIR/bang_hw.prom.$$"
{
  echo "# HELP reaper_ipmi_kcs_ok In-band IPMI (KCS) answers mc info"
  echo "# TYPE reaper_ipmi_kcs_ok gauge"
  if ipmitool mc info >/dev/null 2>&1; then
    echo "reaper_ipmi_kcs_ok 1"
    echo "# TYPE reaper_ipmi_sensor_ok gauge"
    ipmitool sdr 2>/dev/null | awk -F'|' 'NF>=3 {
        n=$1; gsub(/^[ \t]+|[ \t]+$/, "", n); s=$3; gsub(/[ \t]/, "", s)
        gsub(/\\/, "\\\\", n); gsub(/"/, "\\\"", n)
        bad = (s ~ /^(cr|nr|lcr|ucr|lnr|unr)$/)
        printf "reaper_ipmi_sensor_ok{sensor=\"%s\"} %d\n", n, !bad }'
    entries=$(ipmitool sel info 2>/dev/null | awk -F: '/^Entries/ {gsub(/ /, "", $2); print $2}')
    echo "# TYPE reaper_sel_entries gauge"
    echo "reaper_sel_entries ${entries:-0}"
  else
    echo "reaper_ipmi_kcs_ok 0"
  fi
  acked=$(cat "$SEL_ACK_FILE" 2>/dev/null | tr -dc '0-9')
  if [ -n "${entries:-}" ]; then
    if [ -z "$acked" ] && [ ! -e "$SEL_ACK_FILE" ]; then
      acked=$entries; echo "$acked" > "$SEL_ACK_FILE"          # first run: history is not new
    elif [ -n "$acked" ] && [ "$entries" -lt "$acked" ]; then
      acked=0; echo 0 > "$SEL_ACK_FILE"                        # SEL cleared: all since is new
    fi
  fi
  echo "# TYPE reaper_sel_acked_entries gauge"
  echo "reaper_sel_acked_entries ${acked:-0}"
  if [ -x "$DIMMERR" ]; then
    echo "# TYPE reaper_dimm_ce gauge"
    echo "# TYPE reaper_dimm_ue gauge"
    "$DIMMERR" 2>/dev/null | awk '/sn:/ && /ce:/ {
        slot=$1; sn=""; ce=""; ue=""
        for (i=2; i<=NF; i++) { if ($i=="sn:") sn=$(i+1); if ($i=="ce:") ce=$(i+1); if ($i=="ue:") ue=$(i+1) }
        printf "reaper_dimm_ce{slot=\"%s\",serial=\"%s\"} %s\n", slot, sn, ce+0
        printf "reaper_dimm_ue{slot=\"%s\",serial=\"%s\"} %s\n", slot, sn, ue+0 }'
  fi
} > "$TMP"
mv "$TMP" "$TEXTFILE_DIR/bang_hw.prom"
