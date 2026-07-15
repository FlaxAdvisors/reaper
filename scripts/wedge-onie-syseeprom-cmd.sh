#!/usr/bin/env bash
# wedge-onie-syseeprom-cmd.sh — build an `onie-syseeprom -s` command line
# from a Facebook Wedge OpenBMC's /api/sys/mb/fruid endpoint.
#
# Operator workflow: switch is freshly booted into ONIE on a chassis
# whose syseeprom is empty or wrong (e.g. brand-new OEM hardware,
# RMA-replaced motherboard, or a Wedge that came up with its FRU
# wired to the BMC's I2C bus but not yet mirrored into the host-side
# ONIE EEPROM). The BMC already exposes the real Facebook FRU via
# the REST API on :8080, so we read from there and emit the program
# command for the operator to paste at the ONIE busybox shell.
#
# This script only PRINTS the command — it does not run it. ONIE is
# typically reached via serial console (or sol.sh on Wedge BMCs);
# copy-paste is the natural delivery path. A comment block precedes
# the command so the operator can eyeball each field before pasting;
# ONIE's busybox sh treats lines beginning with `#` as comments, so
# the whole block can be pasted as-is.
#
# Usage:
#   scripts/wedge-onie-syseeprom-cmd.sh <bmc-host>[:<port>]
#   Default port: 8080
#
# Mapping (Wedge fruid JSON key → ONIE TLV code):
#   0x21 Product Name      ← Product Name
#   0x22 Part Number       ← Product Part Number
#   0x23 Serial Number     ← Product Serial Number
#   0x24 Base MAC Address  ← Extended MAC Base   (host range start, NOT
#                            "Local MAC" — that's the BMC's own MAC)
#   0x25 Manufacture Date  ← System Manufacturing Date
#                            (MM-DD-YY → MM/DD/YYYY 00:00:00)
#   0x26 Device Version    ← Product Version
#   0x28 Platform Name     ← hardcoded x86_64-accton_wedge100s_32x
#                            (Wedge100S host-side ONL/SONiC platform
#                            string; not in FRU, but stable for the
#                            family this script targets)
#   0x2a MAC Addresses     ← Extended MAC Address Size
#   0x2b Manufacturer      ← System Manufacturer
#   0x2f Service Tag       ← Product Asset Tag
#
# Fields absent from the Wedge FRU (ONIE Version, Country Code,
# Vendor Name, Diag Version) are intentionally omitted; operator can
# append `,0x2d=...` etc. to the printed command if a downstream NOS
# requires them.

set -euo pipefail

target="${1:-}"
if [[ -z "$target" ]]; then
    echo "Usage: $0 <bmc-host>[:<port>]" >&2
    exit 1
fi

if [[ "$target" == *:* ]]; then
    host="${target%:*}"
    port="${target##*:}"
else
    host="$target"
    port="8080"
fi

url="http://${host}:${port}/api/sys/mb/fruid"

fru=$(curl -sf --max-time 10 "$url") || {
    echo "ERROR: failed to fetch $url" >&2
    exit 2
}

# Extract a single Information.<key>, empty string if absent.
get() {
    jq -r --arg k "$1" '.Information[$k] // empty' <<<"$fru"
}

# MM-DD-YY → MM/DD/YYYY 00:00:00. YY<70 → 20YY (heuristic split
# matches the openbmc-era hardware range; no Wedge predates Y2K).
# Unrecognised formats pass through untransformed.
xform_date() {
    local d="$1"
    [[ -z "$d" ]] && return 0
    if [[ "$d" =~ ^([0-9]{2})-([0-9]{2})-([0-9]{2})$ ]]; then
        local mm="${BASH_REMATCH[1]}"
        local dd="${BASH_REMATCH[2]}"
        local yy="${BASH_REMATCH[3]}"
        local yyyy
        if (( 10#$yy < 70 )); then yyyy="20$yy"; else yyyy="19$yy"; fi
        echo "${mm}/${dd}/${yyyy} 00:00:00"
    else
        echo "$d"
    fi
}

declare -A label=(
    [0x21]="Product Name"
    [0x22]="Part Number"
    [0x23]="Serial Number"
    [0x24]="Base MAC Address"
    [0x25]="Manufacture Date"
    [0x26]="Device Version"
    [0x28]="Platform Name"
    [0x2a]="MAC Addresses"
    [0x2b]="Manufacturer"
    [0x2f]="Service Tag"
)

declare -A val=(
    [0x21]="$(get 'Product Name')"
    [0x22]="$(get 'Product Part Number')"
    [0x23]="$(get 'Product Serial Number')"
    [0x24]="$(get 'Extended MAC Base')"
    [0x25]="$(xform_date "$(get 'System Manufacturing Date')")"
    [0x26]="$(get 'Product Version')"
    [0x28]="x86_64-accton_wedge100s_32x"
    [0x2a]="$(get 'Extended MAC Address Size')"
    [0x2b]="$(get 'System Manufacturer')"
    [0x2f]="$(get 'Product Asset Tag')"
)

# Stable order for diff-friendly output and consistent EEPROM TLV layout.
order=(0x21 0x22 0x23 0x24 0x25 0x26 0x28 0x2a 0x2b 0x2f)

# Header block. Stderr-vs-stdout: comments go on stdout too so a
# single redirect captures the full programmable artifact.
printf "# Programming from BMC: %s:%s\n" "$host" "$port"
pairs=()
for code in "${order[@]}"; do
    v="${val[$code]}"
    if [[ -z "$v" ]]; then
        printf "#   %s %-19s : (absent in FRU — skipped)\n" "$code" "${label[$code]}"
        continue
    fi
    printf "#   %s %-19s : %s\n" "$code" "${label[$code]}" "$v"
    pairs+=("${code}=${v}")
done

if [[ ${#pairs[@]} -eq 0 ]]; then
    echo "ERROR: no FRU fields available from $url" >&2
    exit 3
fi

# Single-quote the whole arg: values can contain spaces (date) and
# the comma delimiter must remain unquoted inside the shell word.
# None of the mapped values contain a literal single-quote in any
# observed FRU dump, so simple single-quote wrapping is safe.
arg=$(IFS=,; echo "${pairs[*]}")
printf "onie-syseeprom -s '%s'\n" "$arg"
