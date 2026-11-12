#!/bin/bash
# kea-bmc-denials [minutes]: how many DISCOVERs kea refused for lack of a
# permitted pool (ALLOC_ENGINE_V4_ALLOC_FAIL_CLASSES) -- with the BMC pool
# guard on, each is a BMC with no reservation (spec 2026-09-27 §7). A stuck
# BMC shows up as a steady count for its MAC. One JSON line.
set -uo pipefail
mins="${1:-60}"
case "$mins" in ''|*[!0-9]*) echo "usage: kea-bmc-denials [minutes]" >&2; exit 2;; esac
lines=$(journalctl -u kea-dhcp4 --since "-${mins}min" --no-pager -o cat 2>/dev/null | grep ALLOC_ENGINE_V4_ALLOC_FAIL_CLASSES || true)
total=$(printf '%s' "$lines" | grep -c . || true)
macs=$(printf '%s\n' "$lines" | grep -oE 'hwtype=1 ([0-9a-f]{2}:){5}[0-9a-f]{2}' | awk '{print $2}' | sort | uniq -c \
       | awk 'BEGIN{printf "{"} {printf "%s\"%s\":%d", (NR>1?",":""), $2, $1} END{printf "}"}')
printf '{"window_min":%s,"denials":%s,"by_mac":%s}\n' "$mins" "${total:-0}" "${macs:-{\}}"
