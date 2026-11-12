Dell N3248TE-ON mgmt switch kit: community SONiC + golden L2 config
=====================================================================
For 1G mgmt switches in racks we build for customer sale. 

Files
  sonic-broadcom-202511-20261025.2.bin   SONiC image (202511 = same base as our wedge build)
  sonic-broadcom-202605-20261026.3.bin   newer branch, the fallback
  files/configs/golden/n3248te-l2/config_db.json              golden L2 config (intent only, generic per hwsku)
  n3248te-l2-deploy.sh                   deploy tool (fixed copy of reaper's sonic-l2-deploy.sh)

What the golden config gives you
  - all 54 native ports (Ethernet0-47 1G, 48-51 10G, 52/56 100G) admin-up, untagged in Vlan1000 (one flat bridge)
  - NO breakout (unlike the wedge100s golden): this model's ports are used as built
  - autoneg as on/off (the platform's port_config.ini says 1/0, which SONiC 202511 rejects)
  - FEATURE bgp state=disabled; no per-port IPs, no loopback, no BGP neighbours; NTP = Cloudflare pair
  - no MAC/hostname/serial; the deploy splices in the unit's own DEVICE_METADATA
  Validated: merged with a unit's init_cfg.json + DEVICE_METADATA, it passes the SONiC 202511 YANG check.

Deployment, end to end
  1. Find the switch: under OS6 the OOB port is burned-in MAC +1. Under ONIE/SONiC it is the base MAC.
     For example: `ip neigh | grep -i 18:5a:58`.
  2. Boot ONIE (GRUB > ONIE > Install OS), then ssh root@<ip> (no password).
     `sgdisk -d 3 /dev/sda && partprobe /dev/sda`: the SONiC installer will not delete the existing OS partition.
  3. onie-nos-install http://172.26.0.1/export/share/dell/sonic/sonic-broadcom-202511-20261025.2.bin
     "Could not create partition 3 ... trying the largest aligned block" is benign.
  4. Wait ~5 min after SONiC boots (login: SONiC stock admin). HwSKU "None" and no ports at this point are EXPECTED:
     the stock config fails on autoneg 1/0, and the ASIC fails on scache. The deploy fixes both.
  5. Run the deploy:
       SONIC_PASS='<admin password>' scripts/sonic-l2-deploy-n3248te.sh <mgmt-ip> admin
     It runs these steps:
       - preflight: platform must be x86_64-dellemc_n3248te_c3338-r0, and HwSKU must be DellEMC-N3248TE or the first-boot "None"
       - platform fix 1: autoneg 1/0 -> on/off in the hwsku port_config.ini (backup .orig-autoneg), so anything
         regenerating config from platform defaults passes SONiC 202511's YANG check. The golden's PORT table is
         normalised the same way when it is applied.
       - platform fix 2: stable_size 0x5500000 (85 MiB) -> 0x8000000 (128 MiB) in the hwsku .config.bcm
         (backup .orig-stable_size). Without it syncd fails "Scache out of space" and swss crash-loops.
       Both fixes are idempotent: on a re-run they report "already" and change nothing.
       - backs up /etc/sonic/config_db.json to /home/admin/config_db.backup.<ts>.json
       - installs the golden config with the unit's DEVICE_METADATA spliced in, then `config reload -y -f`
       - waits for the reload process to exit AND APPL_DB PortInitDone (the ports exist)
       - chrony makestep drop-in (/etc/chrony/conf.d/makestep.conf), confirms bgp disabled, `config save`
       - verifies the saved config, 54 ports admin-up (48/4/2), oper-up ports, no bgp container, swss/syncd active, and MACs learned
     It takes ~2-3 min and is safe to re-run. Each run reloads the config (~1 min dataplane blip).
  6. Map cabling with `sudo show lldp table` (front port N = Ethernet N-1) and `sudo show mac`.

Options (environment)
  SONIC_PASS   admin password, also used for sudo. Unset = ssh key auth + passwordless sudo.
  SSH_JUMP     ProxyJump host, e.g. bang-gouda when running from elsewhere.
  GOLDEN       alternate golden file (default: files/configs/golden/n3248te-l2/config_db.json next to the script).
  FORCE=1      skip the platform/hwsku guard.

Revert
  sudo config reload -y /home/admin/config_db.backup.<ts>.json   (on the switch)

Gotchas
  - Use sudo for `show interfaces status`/`show mac`; non-root tracebacks reading the EEPROM.
  - Both platform fixes live on the switch's disk only. A SONiC reinstall loses them, and the deploy re-applies them.
    Tested 2026-10-26 from the stock values (port_config.ini 1/0, stable_size 0x5500000): the script fixed both and the
    ASIC came up. `sonic-cfggen -H -k DellEMC-N3248TE` then generates on/off and passes the YANG check.
  - Fixed vs reaper's sonic-l2-deploy.sh, which still has these bugs:
      (a) its on-box steps are a heredoc fed to `bash -s`, and `redis-cli --scan` in its wait loop reads stdin,
          which swallows the rest of the script. chrony, the bgp disable and `config save` then silently never run.
          Here the on-box steps are copied to the switch as files and run from there.
      (b) "reload complete" could fire after 5 s, mid-reload, because the VLAN was already present.
          Here it waits for the reload PID and for PortInitDone.

In reaper (2026-11-01): golden config here, deploy script scripts/sonic-l2-deploy-n3248te.sh
(the copies under /export/share/dell/sonic on bang-gouda are the originals). Generic per hwsku: no MAC,
hostname or serial; the script splices the unit's own DEVICE_METADATA on-box. A deployed rack's own running
config never goes into reaper.
