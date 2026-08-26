#!/bin/bash -x

# much of this is modified from quanta update scripts included in their FW tarball

cd /export/share/platforms/leopard-v2/smbios/

afubin=./afulnx_64
mod=amifldrv_mod

#### 
#### Function Definitions
####

function help() 
{
    echo "Pass the BIN as argument."
    echo
    echo "For example:"
    echo
    echo "    $0 F06_3B22.BIN [-force]"
    echo
    exit 1
}

function validatemfgandprod() 
{
  #
  # Check board - manufacturer
  #
  MFG_NAME=$(dmidecode -s system-manufacturer | grep -i Quanta)
  if [ -z "$MFG_NAME" ]; then
    echo "This board is not manufactured by Quanta ($MFG_NAME)..."
    echo "use -force parameter to force update"    
    exit 1
  fi

  #
  # if file passed in is .BIN and not .ROM ??
  #
  platform=$(echo $binfile| head -c 10 | tail -c 2)
  case "$platform" in
    ".B")
      #
      # Check board - product name (Leopard)
      #
      PRODUCT_NAME=$(dmidecode -s system-product-name | grep -i Leopard)
      if [ -z "$PRODUCT_NAME" ]; then
        echo "This board is not Leopard ($PRODUCT_NAME)..."
        echo "use -force parameter to force update"
        exit 1
      fi
      ;;
    *)
      echo "File passed was not .BIN."
      help
      ;;
  esac
}

function loadamifldrvmodret() 
{
  #
  # Check for presence of amifldrv_mod.o to run afulnx...
  #
  modprobe $mod
  MODULE=$(lsmod | grep $mod)
  if [ -z "$MODULE" ]; then
    echo "Failed to load module ($mod) required to run $afubin ..."
    echo "You can try to use -force parameter to attempt building."
    echo "(Building unpatched will fail in modern kernels.)"
    return 1
  fi
  return 0
}

function attempttobuild()
{
  # this will fail because the FW doesn't support the newer kernels we are using
  chmod +x $afubin
  $afubin /makedrv
  modprobe $mod
  MODULE=$(lsmod | grep $mod)
  if [ -z "$MODULE" ]; then
    echo "unable to build and load the kernel module needed ($mod)..."
    exit 1
  fi
}

function checkversiondelta()
{
  #
  # do checks if ! force
  #
  currver=$($afubin /s | grep "System ROM ID" | cut -d'=' -f2|sed 's/ //g')
  if [ -z "$currver" ]; then
    echo "Unable to run to check BIOS version ($afubin)"
    echo "use -force parameter to skip check."
    exit 1
  fi

  if [ "$currver" == "$version" ]; then
    echo "desired version ($version) is already current ($currver)"
    echo "user -force parameter to force update..."
    exit 1
  fi

}

function disablewatchdog() 
{
  #
  # Disable nmi_watchdog before execute AFULNX to prevent from CPU lockup.
  #
  if [ -f /proc/sys/kernel/nmi_watchdog ]; then
    NMIWATCHDOG=`cat /proc/sys/kernel/nmi_watchdog`
    echo 0 > /proc/sys/kernel/nmi_watchdog
    echo 300 > /sys/module/rcupdate/parameters/rcu_cpu_stall_timeout
  fi
}

function updatebios()
{
  #
  # Update BIOS
  #
  echo "Update BIOS..."
  $afubin $binfile /P /B /X /N /K
}

function updateme()
{
  #
  # Update ME firmware
  #
  echo "Update ME..."
  $afubin $binfile /ME
}


####
#### Begin Workflow
####

# do we have access to the update utility
if [ ! -e $afubin ]; then
    echo "No $afubin to run to do FW update"
    exit 1
fi

# was an argument passed
if [ -z $1 ]; then
  help
fi

binfile=$1

# is the first argument a file that exists
if [ ! -f $binfile ]; then
  echo "Unable to find file specified ($binfile)..."
  help
fi

version=$(echo $binfile|cut -d'.' -f1 ) # F06_3B22

loadamifldrvmodret
loadretval=$?

#
# Identify platform (Leopard) and manufacturer (Quanta).
#
if [ "$2" == "-force" ];then
  echo "Force update (Skip manufacturer, product name checking, will attempt to build missing kernel module)..."
  if [ $loadretval -ne 0 ]; then
    # failed to load module - attempt to build w/ -force
    attempttobuild
  fi
else
  if [ $loadretval -ne 0 ]; then
    # failed to load module - fail here w/o -force
    exit 1
  fi
  validatemfgandprod
  checkversiondelta
fi 

disablewatchdog

#
#Send first update command
#
ipmitool raw 0x30 0x40 0x01

updatebios
updateme

echo "Reboot system after 5 seconds"
#
#Send second update command
#
sleep 5
ipmitool raw 0x30 0x40 0x02
echo 
echo need force reboot
shutdown -r now
