#!/bin/bash

####
#### Define Common Globals
####

pcidev=""

devinfo=""
devfwver=""
devpsid=""
devsecure=""


####
#### Define Functions
####

function getpcidev()
{
    pcidev=$(lspci -d "15b3:" | cut -d " " -f1 | grep 00.0)
    retval=$?
    if [ -z "$pcidev" ] || [ $retval -ne 0 ]; then
        echo "No Mellanox devices found..."
        exit 0
    fi
}

function checkmstflint()
{
    val=mstflint
    found=$(which $val | grep $val)
    if [ -z "$found" ]; then
        echo "Required package ($val) does not appear to be in path."
        exit 1
    fi
}

function getdevinfo()
{
    devhere=$1
    IFS=$'\n' && devinfo=$(domstflint query $devhere)
    retval=$?
    if [ $retval -eq 0 ]; then
        devfwver=$(printf "%s\n" $devinfo|grep 'FW Version:'|cut -d':' -f2|sed 's/^\s*//')
        devpsid=$(printf "%s\n" $devinfo|grep 'PSID:'|cut -d':' -f2|sed 's/^\s*//')
        devsecure=$(printf "%s\n" $devinfo| grep 'Security Attributes:'|cut -d':' -f2|sed 's/^\s*//')
        echo "devfw $devfwver devpsid $devpsid devsec $devsecure"
    fi
    return $retval
}

function domstconfig()
{
    command=$1
    device=$2
    keyvalue=$3
    cmd="mstconfig -d $device"

    if [ "$command" == "query" ] || [ "$command" == "q" ]; then
        cmd="$cmd $command"
    elif [ "$command" == "set" ] || [ "$command" == "s" ]; then
        cmd="$cmd -y $command $keyvalue"
    else
        echo "Invalid mstconfig command ($command)..."
        exit 1
    fi
    eval "$cmd"
}

function domstflint()
{
    command=$1
    device=$2
    binfile=$3
    cmd="mstflint -d $device"

    if [ "$command" == 'query' ] || [ "$command" == 'q' ]; then
        echo "" # no additional options
    elif [ "$command" == 'burn' ] || [ "$command" == 'b' ]; then
        cmd="$cmd -y -i $binfile"
        if [ $allow_psid_change -ne 0 ]; then
            cmd="$cmd --allow_psid_change"
        fi
        if [ $no_fw_ctrl -ne 0 ]; then
            cmd="$cmd --no_fw_ctrl"
        fi
    else
        echo "Invalid mstflint command ($command)..."
        exit 1
    fi
    eval  "$cmd $command"
}

function domstfwreset()
{
    device=$1
    level=$2
    type=$3
    cmd="mstfwreset -d $device -y"
    
    if [ -n "$level" ]; then
        cmd="$cmd --level $level"
    fi
    if [ -n "$type" ]; then
        cmd="$cmd --type $type"
    fi
    eval "$cmd reset"
}

