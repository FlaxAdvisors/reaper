#!/bin/bash -x

action=$(sed -n '/postaction=/p' /proc/cmdline |sed -re 's/^.*postaction=(\S+).*$/\1/')
# Explicit arg override: the postaction engine advances a staylive-booted node to a
# later phase (e.g. `post.sh postautomate` after Firmware) WITHOUT a reboot -- the
# kernel cmdline still says staylive, but $1 wins. No arg = normal boot flow.
[ -n "$1" ] && action="$1"
[ -z "$action" ] && action="inventory"

# possible actions include:
#   inventory (cfgipmi + cmds for inventory )
#   cfgipmi (only ipmi settings)
#   memtest (mount usb and push memtest files to server)
#   liveboot (just hang out for ssh and post)

echo "Live ISO boot to do: $action"

# Live status on SOL + /run/flax/post-status.json (post_status.sh). A view,
# never the job: every ps_* call hands back the $? it was called with.
. "$(dirname "$0")/post_status.sh"
ps_init "$action"

# postautomate: launch the free-running qualification agent (Wave 4) in its OWN
# transient systemd unit, then stop. banghook.service is Type=oneshot with no
# RemainAfterExit, so its cgroup (and any bare `&`/nohup child) is torn down the
# moment post.sh returns -- systemd-run puts the agent in an independent unit that
# survives. --working-directory pins cwd so the agent's bundled ./dimmerr/./macinv
# etc. relative-path bins resolve. EnvironmentFile (the leading '-' makes it optional)
# carries per-site runtime config rendered into the tarball -- notably FLAX_IPERF_SERVER
# (this site's backup bang). The agent serves the REST contract the flax-post engine
# polls; logs -> `journalctl -u flax-qual-agent`.
if [ "$action" == "postautomate" ]; then
    echo "Launching flax qualification agent (postautomate)"
    ps_begin agent "flax-qual-agent"
    agent_dir="$(cd "$(dirname "$0")" && pwd)"
    systemctl reset-failed flax-qual-agent 2>/dev/null || true
    systemctl stop flax-qual-agent 2>/dev/null || true
    systemd-run --unit=flax-qual-agent --collect \
        --working-directory="$agent_dir" \
        --property="EnvironmentFile=-$agent_dir/flax-qual.env" \
        python3 qual_agent.py
    ps_finish WAITING "flax-qual-agent running -- progress is on the post UI."
    exit 0
fi

ps_begin clock "chronyd + hwclock"
# set the system date via NTP/Chrony
chronyd -q 'pool pool.ntp.org iburst'
hwclock --systohc
ps_done

ps_begin ipmi "dmidecode + openipmi"
# Get the level of IPMI support the system has (if any)
ipmigood=0
manufacturer=$(dmidecode -s system-manufacturer|tr '[:upper:]' '[:lower:]')
if [ -z "$manufacturer" ]; then
    manufacturer=empty
fi
productname=$(dmidecode -s system-product-name|tr '[:upper:]' '[:lower:]')
if [ -z "$productname" ]; then
    productname=empty
fi
system=$(grep ID /etc/os-release|cut -d'=' -f2|head -n1)
if [[ $system =~ ubuntu ]] ; then
    if systemctl --no-pager status openipmi ; then
        if [[ $productname =~ "mono lake" ]]; then
            ipmigood=2
        else
            ipmigood=1
        fi
    fi
elif [[ $system =~ opensuse ]] ; then
    if lsmod | grep -q ipmi_si || systemctl restart ipmi; then
        if [[ $productname =~ "mono lake" ]]; then
            ipmigood=2
        else
            ipmigood=1
        fi
    elif [[ $productname =~ empty ]]; then
        ipmigood=1
    fi
else
    echo "Not ubuntu or opensuse... skipping IPMI"
fi
ps_set ipmigood "$ipmigood"

# use BOOTIF MAC for identity when collecting system data
mac=$(cat /proc/cmdline|sed -re 's/^.*BOOTIF=01-([^ ]+).*$/\1/' | tr -d '-')
if [ -z "$mac" ]; then
    mac=$(cat /sys/class/net/*/address | grep -v 00:00:00:00:00:00 | sort -V | head -n1|tr -d ':')
fi
ps_set mac "$mac"

stamp=$(date  +"%Y%m%d_%H%M%S")
macdir=/tmp/post-${mac}
logdir=${macdir}/$stamp
[ -d $logdir ] || mkdir -p $logdir
ps_set logdir "$logdir"

dst="root@bang"
dstdir="${dst}:/export/nodes/."

# do the IPMI configuration for BOTH "cfgipmi" and "inventory" actions
# skip for liveboot and memtest
if [ $action == "cfgipmi" ] || [ $action == "inventory" ]; then
    # monolake ipmigood == 2 ::: skip cfg
    if [ $ipmigood -eq 1 ]; then
        ./setipmiuser.sh 2>&1 > $logdir/setipmiuser.txt
        ipmitool chassis identify 180
    fi
fi
ps_done "ipmigood=$ipmigood"

# we're ready to exit for BOTH "cfgipmi" and "liveboot" actions
# to leave the node up for more operations and fun
if [ $action == "cfgipmi" ] || [ $action == "liveboot" ]; then
    echo "Live boot tasks complete. Hanging out for SSH or perf tests."
    ps_finish WAITING "live boot tasks done -- up for ssh."
    exit 0
fi

# update fw iff inventory
if [ $action == "inventory" ]; then
    # update quanta leopard firmware
    biosver=F06_3B22.BIN
    quanta=$(echo $manufacturer | grep -i Quanta)
    leopard=$(echo $productname | grep -i Leopard)
    # TP detection: the SYSTEM product-name is the integrator SKU (RECON: Wiwynn
    # "SV7220G3"); the "Tioga Pass" string lives on the BASE BOARD ("Tioga Pass
    # Single Side"). Quanta TPs use yet other strings -- so match an ARRAY of TP
    # identifiers against BOTH system- and baseboard-product-name. Add new
    # Quanta/Wiwynn TP names to TP_NAMES as encountered.
    TP_NAMES=("tioga" "sv7220g3")        # case-insensitive, lowercased below
    boardname=$(dmidecode -s baseboard-product-name | tr '[:upper:]' '[:lower:]')
    tioga=""
    for n in "${TP_NAMES[@]}"; do
        if echo "${productname} ${boardname}" | grep -qiF "$n"; then tioga="yes"; break; fi
    done
    # TP BIOS gate (triage): report this node's in-band BIOS version to the
    # bang. On ACK this powers the node off and the biosfw agent flashes it --
    # so this must come BEFORE any inventory work, and the node comes back on
    # the next boot to collect inventory with the NEW BIOS already in place.
    # Never fatal: no agent / no answer / unreadable version all fall through.
    if [ -n "$tioga" ]; then
        ps_begin biosgate "bios_gate_report.sh"
        ps_state POWER-OFF "biosgate: may power off for a BIOS flash -- not a crash."
        ./bios_gate_report.sh || true
        ps_done
    else
        ps_skip biosgate "not Tioga Pass"
    fi
    if [ -n "$leopard" ] || [ -n "$tioga" ]; then
        # update subset of mellanox nics (per-card PSID->image)
        ps_begin nicfw "update_mellanox.sh"
        ps_state REBOOTING "nicfw: may reset or reboot after flashing a NIC -- not a crash."
        ./update_mellanox.sh
        sleep 5
        ps_done
        if [ -n "$leopard" ] && [ -n "$quanta" ]; then
            ps_begin biosfw "update_quanta_leopard.sh $biosver"
            ps_state REBOOTING "biosfw: reboots if it flashes the BIOS -- not a crash."
            ./update_quanta_leopard.sh $biosver
            # this reboots if update was performed
            ps_done
        else
            ps_skip biosfw "not a Quanta Leopard"
        fi
    else
        ps_skip nicfw "not Leopard/Tioga Pass"
        ps_skip biosfw "not a Quanta Leopard"
    fi
fi
# continue for inventory and memtest
htmlnow=""
dimmdir="/srv/www/htdocs/dimm"
mtdir="/mnt/EFI/BOOT"
if [ $action == "memtest" ]; then
    ps_begin memtest "recover memtest results from USB"

    # this is the dest dir for the rsync
    dstdir="${dst}:${dimmdir}/."

    host=$(hostname)
    if [ -z $host ]; then host=nohostname; fi
    macdir=/tmp/$host
    [ -d $macdir ] || mkdir -p $macdir

    # mount usb stick
    mounted=0
    if [ -b /dev/sda1 ]; then
	mount /dev/sda1 /mnt && sleep 1 && umount /mnt
	sleep 1
	mount /dev/sda1
	if [ $? -eq 0 ]; then mounted=1; fi
	sleep 1
    fi
    
    # recover memtest results
    if [ $mounted -eq 1 ]; then
        lognow=$(ls -1t /mnt/EFI/BOOT/*.log|head -n1)
        htmlnow=$(ls -1t /mnt/EFI/BOOT/*.html|head -n1)
        if [ -z $htmlnow ]; then
            logdir=${macdir}/$stamp
        else
            stamp=$(date -r ${htmlnow} +"%Y%m%d_%H%M%S")
            logdir=${macdir}/$stamp
 	      fi
	
        [ -d $logdir ] || mkdir -p $logdir
        dmidecode 2>&1 > $logdir/dmidecode.txt
        dmesg 2>&1 > $logdir/dmesg.txt
        if [ $ipmigood -ge 1 ]; then
            ipmitool fru 2>&1 > $logdir/ipmitool_fru.txt
        fi

        if [ ! -z $htmlnow ]; then
            mv $lognow $logdir/MemTest.log
            mv $htmlnow $logdir/Temp.html
            iconv -f UTF-16 -t ASCII $logdir/Temp.html > $logdir/MemTest_Report.html
            rm -f ${mtdir}/*.log
            rm -f ${mtdir}/*.html
            rm -f $logdir/Temp.html

            cp ${mtdir}/mt86.cfg  $logdir/.
       	fi
        umount /mnt
    fi
    # live pxe done - power off and wait for manual power on
    #
    # monitor behavior:
    #   power off -> on  && memtestnext/yes is latest: clear the UID indicator, chassis identify 0
    #   power on  -> off && memtestnext/yes is latest: touch memtestnext/no, set bootdev pxe, set ipmi power on
    #
    [ -d $macdir/states/memtestnext ] || mkdir -p $macdir/states/memtestnext
    touch $macdir/states/memtestnext/yes
    ps_done
    ps_begin ident "chassis identify force"
    ipmitool chassis identify force
    ps_done
else

# AMI BIOS leopard specific config dump
#./SCELNX_64 /o /s $logdir/nvscript.txt /h $logdir/hiidump.txt

# amtinventory will do all of the following 
# get nic info and link up early to allow time for LLDP neighbor info
ps_begin inventory "nics + lldp (1 of 6)"
for dev in /sys/class/net/*
do
    dev=$(basename $dev)
    if [ $dev == "lo" ]; then continue ; fi
    ethtool $dev > $logdir/ethtool_${dev}.txt
    ethtool -i $dev > $logdir/ethtool-i_${dev}.txt
    ip link set ${dev} up
    systemctl start lldpd
done

ps_note "hostname, blkid, dmesg, dmidecode (2 of 6)"
hostname > $logdir/hostname.txt
blkid 2>&1 > $logdir/blkid.txt
dmesg > $logdir/dmesg.txt
dmidecode 2>&1 > $logdir/dmidecode.txt
ps_note "hwinfo (3 of 6)"
hwinfo --arch --bios --block --bridge --cdrom --cpu --disk --framebuffer --gfxcard --hub --ide --keyboard --memory --mmc-ctrl --monitor --mouse --netcard --network --partition --pci --pcmcia --pcmcia-ctrl --scsi --smp --storage-ctrl --sys --tape --tv --uml --usb --usb-ctrl --vbe --wlan --xen --zip 2>&1 > $logdir/hwinfo.txt
ps_note "ipmitool fru, sdr (4 of 6)"
if [ $ipmigood -ge 1 ]; then
    ipmitool fru 2>&1 > $logdir/ipmitool_fru.txt
    ipmitool sdr elist 2>&1 > $logdir/ipmitool_sdr_elist.txt
fi
ps_note "ipmitool mc, sel, lan, user, sensor -- sel can take long on a flooded SEL (5 of 6)"
if [ $ipmigood -eq 1 ]; then
    ipmitool mc info 2>&1 > $logdir/ipmitool_mc_info.txt
    ipmitool sel elist 2>&1 > $logdir/ipmitool_sel_elist.txt
    ipmitool lan print 1 2>&1 > $logdir/ipmitool_lan_print_1.txt
    ipmitool user list 1 2>&1 > $logdir/ipmitool_user_list_1.txt
    ipmitool lan print 8 2>&1 > $logdir/ipmitool_lan_print_8.txt
    ipmitool sensor list all 2>&1 > $logdir/ipmitool_sensor_list_all.txt
fi
ps_note "lldp, lsblk, lscpu, lspci, lsusb, lsscsi, ip, /proc (6 of 6)"
lldpcli show neigh 2>&1 > $logdir/lldpcli-show-neigh.txt
lsblk 2>&1 > $logdir/lsblk.txt
lscpu 2>&1 > $logdir/lscpu.txt
lspci -vv 2>&1 > $logdir/lspci-vv.txt
lspci -vvv 2>&1 > $logdir/lspci-vvv.txt
ls -lR /dev/disk 2>&1 > $logdir/ls-lR_dev_disk.txt
lsusb -v 2>&1 > $logdir/lsusb-v.txt
lsscsi -c 2>&1 > $logdir/lsscsi-c.txt
lsscsi -g 2>&1 > $logdir/lsscsi-g.txt
ip -d link 2>&1 > $logdir/ip-d_link.txt
ip -d address 2>&1 > $logdir/ip-d_address.txt
cat /proc/cpuinfo 2>&1 > $logdir/cpuinfo.txt
cat /proc/meminfo 2>&1 > $logdir/meminfo.txt
cat /proc/scsi/scsi 2>&1 > $logdir/scsi.txt
# Refresh /opt/flax/bin from the bang before the tool calls below.
#
# These are invoked by absolute path, but /opt/flax/bin on a live-booted DUT is
# populated by a SEPARATE ansible stage (ghost flax_user.yml, donum 54) whose
# with_items list can drift from what post.sh needs -- or stop running at all.
# It did: a tab-indented list item made that playbook unparseable for two
# commits, so dimmerr never reached a single node and dimmerr.txt came back
# 0 bytes on every fresh inventory while dimmsum/lsnet/bootorder beside it were
# fine (they had been pushed by the last parseable version).
#
# ssh/rsync to root@bang is the same path the dump rsync at the end of this
# script already uses, so this adds no new dependency or credential. -a without
# --delete only adds and updates; nothing on the node is removed.
#
# Best-effort on purpose: a DUT that cannot reach the bang must still complete
# its inventory, so on failure fall back to the copies post.tgz bundles
# alongside this script -- which is exactly the six tools invoked below.
ps_done
ps_begin binrefresh "rsync /opt/flax/bin from the bang"
mkdir -p /opt/flax/bin
if ! rsync -a --timeout=20 \
        -e "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
        ${dst}:/opt/flax/bin/ /opt/flax/bin/ ; then
    echo "post.sh: bin refresh from ${dst} failed -- falling back to the post.tgz bundle"
    ps_note "bang unreachable -- using the post.tgz copies"
    for b in macinv dimmerr dimmsum lsnet alldisks bootorder ; do
        [ -f "$(dirname "$0")/$b" ] && install -m 0755 "$(dirname "$0")/$b" /opt/flax/bin/
    done
fi
ps_done

ps_begin tools "dimmsum (1 of 7)"
/opt/flax/bin/dimmsum     2>&1 > $logdir/dimmsum.txt
# dimmsum is DIMM *inventory* (size/locator/mfg/serial/part/speed from
# dmidecode); dimmerr is DIMM *health* -- per-DIMM EDAC correctable and
# uncorrectable counts mapped to the BIOS silkscreen locator + serial. Until
# now the only way to see CE/UE was post stage 61 running dimmerr ad hoc over
# ansible and printing to the operator's terminal, so nothing durable landed in
# the node dump and the triage UI could not show DIMM health at all.
# Degrades quietly: no EDAC nodes under /sys (module not loaded, or a platform
# EDAC does not cover) means the glob matches nothing and the file is empty --
# the same "absent, not healthy" signal an empty smartctl--all.txt carries.
ps_note "dimmerr (2 of 7)"
/opt/flax/bin/dimmerr     2>&1 > $logdir/dimmerr.txt
ps_note "alldisks (3 of 7)"
/opt/flax/bin/alldisks -v 2>&1 > $logdir/alldisks-v.txt
ps_note "lsnet (4 of 7)"
/opt/flax/bin/lsnet       2>&1 > $logdir/lsnet.txt
ps_note "bootorder (5 of 7)"
/opt/flax/bin/bootorder   2>&1 > $logdir/bootorder.txt
ps_note "collect_mellanox (6 of 7)"
./collect_mellanox.sh $logdir

ps_note "smartctl (7 of 7)"
for dev in $(smartctl --scan | cut -d' ' -f1)
do
    smartctl --all $dev
done > $logdir/smartctl--all.txt
ps_done

ps_begin ident "chassis identify 180"
ipmitool chassis identify 180
ps_done

fi # end of if memtest else clause (inventory section)

#if [ -d .ssh ] ; then
#    echo "Working with .ssh in $PWD"
#    chown -R root:root .ssh
#    if [ -d /root/.ssh ] ; then
#       mv /root/.ssh /root/.ssh.orig
#    fi
#    mv .ssh /root/.ssh
#fi
pushd $macdir
rm -f latest
ln -s $stamp latest
popd

if [ $action == "memtest" ]; then
    scrlog="screenlog.${host}.0"
    hostdir=${dimmdir}/${host}
    scrsrc="${hostdir}/${scrlog}"
    scrdst="${hostdir}/${stamp}/${scrlog}"
    ssh $dst "[ -f $scrsrc ] && mkdir -p ${hostdir}/${stamp} && mv $scrsrc $scrdst"
    ssh $dst "solcapture -r ${host}"
    # have to create the dst latest with initial sync
    rsync -SHAXav $macdir $dstdir

    if [ -z $htmlnow ]; then
	ssh $dst "process_results $host failed"
    else
        ssh $dst "process_results $host"
    fi
fi
journalctl -la -u banghook > $logdir/banghook.log
ps_begin dump "rsync to bang:/export/nodes"
dumpok=0
if rsync -SHAXav $macdir $dstdir; then
    dumpok=1
    ps_done
else
    ps_fail "rsync to the bang failed (rc $?) -- results are only on this node"
fi

## + chown -R root:root .ssh
## chown: cannot access '.ssh': No such file or directory
## + '[' -d /root/.ssh ']'
## + mv .ssh /root/.ssh
## mv: cannot stat '.ssh': No such file or directory
## + rsync -SHAXav /tmp/post-248a07883516 root@bang:/export/nodes/.
## Host key verification failed.
## rsync: connection unexpectedly closed (0 bytes received so far) [sender]
## rsync error: unexplained error (code 255) at io.c(235) [sender=3.1.2]
## + '[' 1 -ne 0 ']'
## + ipmitool chassis identify 180

# The last word on SOL. Collecting inventory is NOT a verdict (operator,
# 2026-09-25): population and errors are judged in the GUI, so never let this
# read as "done, pull it".
if [ "$dumpok" = 1 ] && [ "$action" == "inventory" ]; then
    donemsg="INVENTORY COLLECTED - check the GUI for population and errors !!! - CHECK THE GUI"
elif [ "$dumpok" = 1 ]; then
    donemsg="$action results collected - CHECK THE GUI"
else
    donemsg="$action results NOT delivered to the bang (dump failed) - CHECK THE GUI and the banghook journal"
fi
ps_begin poweroff
ps_done
ps_finish POWER-OFF "$donemsg"
# Give the final frame time to reach SOL: without this the power cut beat the
# repaint and the last thing SOL ever showed was "RUNNING dump" (et24b3).
sleep 3
if [ $ipmigood -eq 1 ]; then
    echo "using IPMI to power off."
    ipmitool chassis power off
else
    echo "No IPMI on this system? Invoking shutdown."
    shutdown -h now
fi
