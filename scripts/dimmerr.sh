#!/bin/bash
# Per-DIMM EDAC error counts (CE + UE) mapped to BIOS silkscreen locator + serial.
# EDAC dimm_label -> SMBIOS Bank Locator _Node{S}_Channel{N}_Dimm{D} -> Locator (silkscreen) + Serial
#   skx_edac (Skylake/Cascade-SP, Tioga Pass):  CPU_SrcID#S_MC#M_Chan#C_DIMM#D  N = M*3+C (2 iMC x 3 ch)
#   sb_edac  (Haswell/Broadwell-EP, Leopard):   CPU_SrcID#S_Ha#H_Chan#C_DIMM#D  N = H*2+C (2 HA x 2 ch;
#            a 1-HA die has only Ha#0 with Chan 0-3, same formula). No MC# in this label: parsing it
#            as MC# read Ha#1 as channels 0/1 and doubled A0/A2/B0/B2 (et23b3 2026-11-05).
declare -A SILK SN
while IFS=$'\t' read -r bank silk sn; do SILK[$bank]=$silk; SN[$bank]=$sn; done < <(
  sudo -n dmidecode -t memory 2>/dev/null | awk '
    /^[ \t]*Locator:/ && $0 !~ /Bank/ {sub(/^[ \t]*Locator:[ \t]*/,""); s=$0; bank=""; sn=""}
    /^[ \t]*Bank Locator:/ {sub(/^[ \t]*Bank Locator:[ \t]*/,""); bank=$0}
    /^[ \t]*Serial Number:/ {sub(/^[ \t]*Serial Number:[ \t]*/,""); sn=$0; if (bank!="") print bank"\t"s"\t"sn}')
for d in /sys/devices/system/edac/mc/mc*/dimm*; do
  [ -f "$d/dimm_label" ] || continue
  lbl=$(cat "$d/dimm_label")
  S=${lbl#*SrcID#}; S=${S%%_*}
  C=${lbl#*Chan#};  C=${C%%_*}
  D=${lbl##*DIMM#}
  if [[ $lbl == *_Ha#* ]]; then
    H=${lbl#*_Ha#}; H=${H%%_*}; N=$((H*2+C))
  else
    M=${lbl#*MC#};  M=${M%%_*}; N=$((M*3+C))
  fi
  key=_Node${S}_Channel${N}_Dimm${D}
  printf "%-9s sn: %-14s ce: %-4s ue: %s\n" "${SILK[$key]:-$lbl}" "${SN[$key]:-?}" \
    "$(cat "$d/dimm_ce_count")" "$(cat "$d/dimm_ue_count")"
done
