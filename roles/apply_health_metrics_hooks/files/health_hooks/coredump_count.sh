#!/bin/bash
# Counts coredumps per-EXE captured by systemd-coredump.
# Reset semantics: cumulative count since the systemd-coredump journal
# began. Prometheus rate() handles deltas, so cumulative is the right shape.

set -u
umask 0022
TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector
HOST=$(hostname -s)

TMP="$TEXTFILE_DIR/coredumps.prom.$$"
{
  echo "# HELP coredump_count Total coredumps captured by systemd-coredump, by exe"
  echo "# TYPE coredump_count counter"
  emitted=0
  if command -v coredumpctl >/dev/null 2>&1; then
    while read count exe; do
      exe_label=$(basename "$exe")
      echo "coredump_count{bang=\"$HOST\",exe=\"$exe_label\"} $count"
      emitted=1
    done < <(coredumpctl list --no-pager --no-legend 2>/dev/null \
               | awk '{print $(NF-1)}' \
               | sort \
               | uniq -c)
    # Note: coredumpctl columns are TIME(5 words) PID UID GID SIG COREFILE EXE SIZE.
    # $(NF-1) selects EXE. Previous $NF was selecting SIZE (e.g. "172.3K") and
    # emitting that as the exe label — visible in Prometheus as bogus series like
    # coredump_count{exe="172.3K"}. Fixed 2026-05-17 once the bug surfaced via dashboard.
  fi
  # Zero-baseline so the series always exists (dashboards render "0"
  # rather than "No data" on a healthy host).
  if [ "$emitted" -eq 0 ]; then
    echo "coredump_count{bang=\"$HOST\",exe=\"_none\"} 0"
  fi
} > "$TMP"
mv "$TMP" "$TEXTFILE_DIR/coredumps.prom"
