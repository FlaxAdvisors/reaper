#!/bin/bash
# bmc-reset-via-redfish.sh
#
# Reset a BMC via Redfish Manager.Reset (ForceRestart) — the recovery
# path for the recurring "Error in open session response message :
# insufficient resources for session" symptom on AMI MegaRAC BMCs
# (and any BMC whose IPMI session table fills up). See TODO.md
# "AMI MegaRAC IPMI session exhaustion (recurring)".
#
# Idempotent in effect: ForceRestart is always safe — if the BMC's
# fine, it just reboots. ~3 min downtime, host CPU stays up.
#
# Usage: bmc-reset-via-redfish.sh <bmc-host> [<user> <pass>] [--dry-run]
#   user defaults to Administrator, pass defaults to superuser
#   (the AMI MegaRAC working pair in this fleet — see
#   /etc/flax/credentials-redfish.json for the canonical cred list).
#   --dry-run: verify auth and resolve the Manager ID, don't reset.
#
# Exit codes:
#   0 — reset succeeded and BMC came back
#   1 — Redfish auth failed (cred wrong or BMC down)
#   2 — reset POST rejected
#   3 — BMC did not return within 5 minutes after reset

set -euo pipefail

bmc=""
user="Administrator"
pass="superuser"
dry_run=false

for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=true ;;
        *)
            if [[ -z "$bmc" ]];   then bmc="$arg"
            elif [[ "$user" == "Administrator" && "$pass" == "superuser" ]]; then
                # First positional after bmc is user
                user="$arg"
            elif [[ "$pass" == "superuser" ]]; then
                pass="$arg"
            fi
            ;;
    esac
done

if [[ -z "$bmc" ]]; then
    echo "Usage: $0 <bmc-host> [<user> <pass>] [--dry-run]" >&2
    exit 1
fi

for cmd in curl python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "$cmd not found in PATH" >&2
        exit 1
    fi
done

# 1. Verify Redfish auth before firing anything destructive.
http=$(curl -sk -u "$user:$pass" --max-time 5 -o /dev/null \
       -w "%{http_code}" "https://$bmc/redfish/v1/Managers" 2>&1)
if [[ "$http" != "200" ]]; then
    echo "[$bmc] Redfish GET /Managers returned HTTP $http (cred wrong or BMC down)" >&2
    exit 1
fi

# 2. Resolve the Manager @odata.id. AMI uses "Self", Phosphor uses
#    "bmc". Members list is the source of truth either way.
mgr=$(curl -sk -u "$user:$pass" --max-time 5 \
      "https://$bmc/redfish/v1/Managers" 2>/dev/null | \
      python3 -c '
import json, sys
d = json.load(sys.stdin)
ms = d.get("Members", [])
if ms:
    print(ms[0]["@odata.id"])
')
if [[ -z "$mgr" ]]; then
    echo "[$bmc] no Manager members in /redfish/v1/Managers" >&2
    exit 1
fi
echo "[$bmc] Manager: $mgr"

if $dry_run; then
    echo "[$bmc] --dry-run: not posting reset"
    exit 0
fi

# 3. POST Manager.Reset ForceRestart.
echo "[$bmc] posting Manager.Reset ForceRestart ..."
http=$(curl -sk -u "$user:$pass" --max-time 10 -X POST \
       -H "Content-Type: application/json" \
       -d '{"ResetType":"ForceRestart"}' \
       -o /dev/null -w "%{http_code}" \
       "https://$bmc${mgr}/Actions/Manager.Reset" 2>&1)

# AMI returns 204 No Content; some BMCs return 200 or 202 Accepted.
case "$http" in
    200|202|204) echo "[$bmc] reset accepted (HTTP $http)" ;;
    *) echo "[$bmc] reset POST returned HTTP $http" >&2; exit 2 ;;
esac

# 4. Poll for BMC reachability — Redfish 200 means it's back.
echo "[$bmc] waiting for BMC to come back (max 5 min)..."
for i in $(seq 1 60); do
    sleep 5
    http=$(curl -sk -u "$user:$pass" --max-time 3 -o /dev/null \
           -w "%{http_code}" "https://$bmc/redfish/v1/Managers" 2>/dev/null || echo 000)
    if [[ "$http" == "200" ]]; then
        echo "[$bmc] back online after $((i*5))s"
        exit 0
    fi
done

echo "[$bmc] BMC did not return Redfish 200 within 5 min" >&2
exit 3
