"""collect_bmc_usbnet.sh against a fake sysfs + stub modprobe/ip/ping6/sysctl."""
import os
import stat
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "collect_bmc_usbnet.sh")

HOST_LL = "fe80::ff:feaa:bb02"
BMC_LL = "fe80::ff:feaa:bb01"
ALLNODES = ("PING ff02::1%eth1 (ff02::1%eth1) 56 data bytes\n"
            "64 bytes from " + HOST_LL + "%eth1: icmp_seq=1 ttl=64 time=0.079 ms\n"
            "64 bytes from " + BMC_LL + "%eth1: icmp_seq=1 ttl=64 time=1.03 ms\n")
UNICAST = ("64 bytes from " + BMC_LL + "%eth1: icmp_seq=1 ttl=64 time=0.6 ms\n"
           "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
           "rtt min/avg/max/mdev = 0.500/0.600/0.700/0.080 ms\n")


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


def _usb_intf(sysfs, triplet=("02", "02", "ff")):
    dev = os.path.join(sysfs, "bus/usb/devices/1-4.5")
    intf = dev + ":1.0"
    _write(dev + "/idVendor", "046b\n")
    _write(dev + "/idProduct", "ffb0\n")
    _write(dev + "/product", "Virtual Ethernet\n")
    for name, val in zip(("bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol"), triplet):
        _write(os.path.join(intf, name), val + "\n")
    return intf


def _stubs(tmp, intf, *, netdev=True, allnodes=ALLNODES, unicast_rc=0):
    """modprobe creates the netdev under the interface (as the kernel does)."""
    bindir = os.path.join(tmp, "bin")
    x = stat.S_IRWXU
    mk = ('mkdir -p "%s/net/eth1" && echo 02:00:00:aa:bb:02 > "%s/net/eth1/address"'
          % (intf, intf)) if netdev else "true"
    _write(bindir + "/modprobe", "#!/bin/bash\necho \"$@\" >> %s/modprobe.log\n%s\n" % (tmp, mk), x)
    _write(bindir + "/sysctl", "#!/bin/bash\nexit 0\n", x)
    _write(bindir + "/ip", "#!/bin/bash\n"
           "case \"$*\" in *addr*) echo '5: eth1    inet6 %s/64 scope link proto kernel_ll';; esac\n"
           % HOST_LL, x)
    _write(bindir + "/ping6", "#!/bin/bash\n"
           "case \"$*\" in *ff02::1*) printf '%%s' \"$ALLNODES\"; exit 0;;\n"
           "*) printf '%%s' \"$UNICAST\"; exit %d;; esac\n" % unicast_rc, x)
    return bindir, {"ALLNODES": allnodes, "UNICAST": UNICAST}


def _run(tmp, sysfs, bindir=None, extra=None):
    env = dict(os.environ, BMC_USBNET_SYSFS=sysfs, BMC_USBNET_WAIT_S="1")
    if bindir:
        env["PATH"] = bindir + os.pathsep + env["PATH"]
    env.update(extra or {})
    p = subprocess.run(["bash", SCRIPT], env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
    head = p.stdout.split("----- raw")[0]
    kv = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
    return p.returncode, kv, p.stdout


def test_no_usb_nic_reports_absent(tmp_path):
    sysfs = str(tmp_path / "sys")
    os.makedirs(sysfs + "/bus/usb/devices/1-4:1.0")
    _write(sysfs + "/bus/usb/devices/1-4:1.0/bInterfaceClass", "09\n")   # a hub
    rc, kv, _ = _run(str(tmp_path), sysfs)
    assert rc == 0 and kv["present"] == "no" and kv["verdict"] == "absent"


def test_bmc_answers_on_link_local(tmp_path):
    sysfs = str(tmp_path / "sys")
    intf = _usb_intf(sysfs)
    bindir, env = _stubs(str(tmp_path), intf)
    rc, kv, out = _run(str(tmp_path), sysfs, bindir, env)
    assert rc == 0, out
    assert kv["verdict"] == "ok", out
    assert kv["vendor_product"] == "046b:ffb0" and kv["kind"] == "rndis"
    assert kv["module"] == "rndis_host rc=0"
    assert kv["iface"] == "eth1" and kv["iface_mac"] == "02:00:00:aa:bb:02"
    assert kv["host_ll"] == HOST_LL
    assert kv["bmc_ll"] == BMC_LL and kv["bmc_ll_expected"] == "yes"   # own reply filtered out
    assert kv["loss"] == "0%" and kv["rtt"] == "0.500/0.600/0.700/0.080 ms"
    assert open(str(tmp_path / "modprobe.log")).read().split() == ["rndis_host"]


def test_only_our_own_reply_means_no_bmc(tmp_path):
    sysfs = str(tmp_path / "sys")
    intf = _usb_intf(sysfs)
    only_self = "64 bytes from " + HOST_LL + "%eth1: icmp_seq=1 ttl=64 time=0.07 ms\n"
    bindir, env = _stubs(str(tmp_path), intf, allnodes=only_self)
    rc, kv, out = _run(str(tmp_path), sysfs, bindir, env)
    assert rc == 0 and kv["verdict"] == "no_bmc" and kv["bmc_ll"] == "none", out


def test_driver_binds_no_netdev(tmp_path):
    sysfs = str(tmp_path / "sys")
    intf = _usb_intf(sysfs)
    bindir, env = _stubs(str(tmp_path), intf, netdev=False)
    rc, kv, out = _run(str(tmp_path), sysfs, bindir, env)
    assert rc == 0 and kv["verdict"] == "no_iface" and kv["iface"] == "none", out


def test_unicast_ping_failure_is_bmc_unreachable(tmp_path):
    sysfs = str(tmp_path / "sys")
    intf = _usb_intf(sysfs)
    bindir, env = _stubs(str(tmp_path), intf, unicast_rc=1)
    rc, kv, out = _run(str(tmp_path), sysfs, bindir, env)
    assert rc == 0 and kv["verdict"] == "bmc_unreachable" and kv["ping_rc"] == "1", out


def test_cdc_ncm_loads_cdc_ncm(tmp_path):
    sysfs = str(tmp_path / "sys")
    intf = _usb_intf(sysfs, ("02", "0d", "00"))
    bindir, env = _stubs(str(tmp_path), intf)
    rc, kv, out = _run(str(tmp_path), sysfs, bindir, env)
    assert kv["kind"] == "ncm" and kv["module"] == "cdc_ncm rc=0", out
