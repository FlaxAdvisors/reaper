#!/bin/bash

source ./common_mellanox.sh

logdir=$1
pcidev=""

getpcidev

# loop over mlx devices
for mlxdev in $(printf "%s\n" $pcidev); do
    # get the info about the PSID and version from the hardware
    domstflint query $mlxdev 2>&1 > $logdir/mstflint-d_${mlxdev}_query.txt
    domstconfig query $mlxdev 2>&1 > $logdir/mstconfig-d_${mlxdev}_query.txt
done

# Summarize per-card FW-lock state for the Triage UI padlock badge.
python3 ./nic_fw_lock.py "$logdir" > "$logdir/nic_fw_lock.json"
