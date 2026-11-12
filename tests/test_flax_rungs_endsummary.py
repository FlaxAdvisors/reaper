"""flax_rungs.endsummary (Done's end-summary rung) + redfish.clear_boot_override."""
import json
import os

import pytest

pytest.importorskip("flax_rungs.endsummary")
from flax_rungs import endsummary, redfish

HOST_PW, BMC_PW = "fake-host-pw", "fake-bmc-pw"
CREDS = [("admin", BMC_PW)]
BENIGN = ("   1 | 10/29/2026 | 10:00:01 | Temperature Inlet Temp | Upper Critical going high | Asserted\n"
          "   2 | 10/29/2026 | 10:00:02 | Temperature C1 Remote Temp | Upper Critical going high | Asserted\n"
          "   3 | 10/29/2026 | 10:00:03 | System Event #0x1 | Unknown Chassis Pwr Sts | Asserted\n")
IERR = "   4 | 10/29/2026 | 10:00:04 | Processor #0x1 | IERR | Asserted"
INBAND = ("===flax-es net===\neno1 UP 02:00:5e:00:53:01\neno1 UP 172.32.37.1/16\n"
          "===flax-es-rc net 0\n"
          "===flax-es disks===\nnvme0n1 \"Sandisk SSD PLUS\" SN1 1.7T disk\n"
          "===flax-es-rc disks 0\n"
          "===flax-es bootorder===\nBootOrder: 0001,0002\n"
          "===flax-es-rc bootorder 0\n"
          "===flax-es dimms===\nMemory Device\n\tSize: 32 GB\n"
          "===flax-es-rc dimms 0\n")


class Rig:
    """ipmitool answers on cipher `cipher` (None: never); ssh exits `ssh_rc`."""

    def __init__(self, sel=BENIGN, cipher=17, ssh_rc=0, sel_rc=0, inband=INBAND):
        self.sel, self.cipher, self.ssh_rc, self.sel_rc = sel, cipher, ssh_rc, sel_rc
        self.inband, self.calls = inband, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[0] == "sshpass":
            if self.ssh_rc:
                return _P(self.ssh_rc, "", "ssh: connect to host 172.32.37.1 port 22: No route")
            return _P(0, self.inband)
        assert argv[0] == "ipmitool" and kw["env"]["IPMI_PASSWORD"] == BMC_PW
        if self.cipher is None or argv[argv.index("-C") + 1] != str(self.cipher):
            return _P(1, "", "Error: Unable to establish IPMI v2 / RMCP+ session")
        args = argv[argv.index("-E") + 1:]
        if args == ["chassis", "power", "status"]:
            return _P(0, "Chassis Power is on\n")
        if args == ["sel", "elist"]:
            return _P(self.sel_rc, self.sel if not self.sel_rc else "", "")
        if args == ["sdr", "elist"]:
            return _P(0, "Inlet Temp | 01h | ok | 7.1 | 24 degrees C\n")
        raise AssertionError(args)


class _P:
    def __init__(self, rc, out, err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _collect(rig, host_ip="172.32.37.1"):
    return endsummary.collect(host_ip, "172.32.37.11", host_user="flax", host_pw=HOST_PW,
                              bmc_creds=CREDS, runner=rig)


def test_sel_benign_filtered():
    r = _collect(Rig(sel=BENIGN + IERR + "\n"))
    assert r.verdict == "fail"
    assert r.detail["sel_nonbenign"] == [IERR]
    assert "IERR" in r.detail["headline"]
    assert r.artifacts["sel-elist.txt"].endswith(IERR + "\n")


def test_sel_clean_passes():
    r = _collect(Rig(sel=BENIGN))
    assert r.verdict == "pass" and r.detail["sel_nonbenign"] == []
    assert r.detail["inband"] and r.detail["oob"]
    for name in ("net.txt", "disks.txt", "bootorder.txt", "dimms.txt", "sdr-elist.txt",
                 "sel-elist.txt"):
        assert r.artifacts[name].strip(), name
    assert "Sandisk SSD PLUS" in r.artifacts["disks.txt"]
    # an empty SEL is read and clean
    assert _collect(Rig(sel="SEL has no entries\n")).verdict == "pass"


def test_sel_on_cipher_3():
    r = _collect(Rig(sel=BENIGN, cipher=3))
    assert r.verdict == "pass" and r.detail["cipher"] == 3


def test_bmc_unreachable_is_unmeasured():
    rig = Rig(cipher=None)
    r = _collect(rig)
    assert r.verdict == "unmeasured" and r.detail["oob"] is False
    assert r.detail["inband"] is True             # the host half still ran
    assert "sel-elist.txt" not in r.artifacts
    # a session that answered but whose `sel elist` failed is not a clean SEL either
    assert _collect(Rig(sel_rc=1)).verdict == "unmeasured"


def test_host_unreachable_still_reads_sel():
    r = _collect(Rig(sel=BENIGN, ssh_rc=255))
    assert r.verdict == "pass" and r.detail["inband"] is False
    assert any("in-band" in k and "255" in v for k, v in r.detail["rows"])
    assert "net.txt" not in r.artifacts and "sel-elist.txt" in r.artifacts
    r = _collect(Rig(sel=BENIGN + IERR + "\n", ssh_rc=255))
    assert r.verdict == "fail"


def test_inband_section_failure_keeps_the_others():
    out = INBAND.replace("BootOrder: 0001,0002\n", "bash: efibootmgr: command not found\n") \
                .replace("rc bootorder 0", "rc bootorder 127")
    r = _collect(Rig(sel=BENIGN, inband=out))
    assert r.verdict == "pass" and r.detail["inband"] is True
    assert {"net.txt", "disks.txt", "dimms.txt"} <= set(r.artifacts)
    assert "command not found" in r.artifacts["bootorder.txt"]
    assert set(r.detail["inband_errors"]) == {"bootorder"}
    assert "rc=127" in r.detail["inband_errors"]["bootorder"]
    assert any(k == "in-band bootorder" for k, _v in r.detail["rows"])
    # the script runs every section and ends 0: one failure never hides the rest
    script = endsummary._script()
    assert script.rstrip().endswith("exit 0") and script.count("===flax-es-rc ") == 4


def test_first_boot_entry():
    pxe = ("BootCurrent: 0003\nBootOrder: 0001,0003\n"
           "Boot0001* UEFI: PXE IP4 Mellanox\tPciRoot(0x0)/MAC(98039b000001,1)/IPv4(0.0.0.0)\n"
           "Boot0003* ubuntu\tHD(1,GPT,abc)/File(\\EFI\\ubuntu\\shimx64.efi)\n")
    assert endsummary.first_boot(pxe)[0] == "0001" and endsummary.first_boot(pxe)[2] is True
    disk = pxe.replace("BootOrder: 0001,0003", "BootOrder: 0003,0001")
    bid, line, net = endsummary.first_boot(disk)
    assert (bid, net) == ("0003", False) and line.startswith("Boot0003* ubuntu")
    assert endsummary.first_boot("") == (None, None, False)


def test_no_host_ip_skips_inband():
    rig = Rig(sel=BENIGN)
    r = _collect(rig, host_ip=None)
    assert r.verdict == "pass" and r.detail["inband"] is False
    assert not [a for a, _ in rig.calls if a[0] == "sshpass"]
    assert any(v.startswith("skipped") for k, v in r.detail["rows"] if k == "in-band")


def test_no_password_in_argv():
    rig = Rig(sel=BENIGN + IERR + "\n")
    _collect(rig)
    assert rig.calls
    for argv, kw in rig.calls:
        joined = " ".join(argv)
        assert HOST_PW not in joined and BMC_PW not in joined
        assert HOST_PW not in (kw.get("input") or "")
    ssh = [kw for a, kw in rig.calls if a[0] == "sshpass"]
    assert ssh and ssh[0]["env"]["SSHPASS"] == HOST_PW


# ---------------------------------------------------------------- clear_boot_override
def test_clear_override_body():
    seen = []

    def ok(argv, **kw):
        seen.append((argv, os.read(kw["pass_fds"][0], 4096).decode()))
        return _P(0, "204")
    r = redfish.clear_boot_override("172.32.37.11", CREDS, runner=ok)
    assert r.verdict == "pass"
    assert r.detail == {"method": "redfish", "user": "admin", "target": "None"}
    argv, netrc = seen[0]
    body = json.loads(argv[argv.index("-d") + 1])["Boot"]
    assert body["BootSourceOverrideEnabled"] == "Disabled"
    assert body["BootSourceOverrideTarget"] == "None"
    assert "PATCH" in argv and BMC_PW not in " ".join(argv) and BMC_PW in netrc

    r = redfish.clear_boot_override("172.32.37.11", CREDS + [("root", "fake-2")],
                                    runner=lambda a, **kw: _P(0, "401"))
    assert r.verdict == "fail" and "http 401" in r.detail["error"]


# R37 / L05, 2026-10-30: thresholds that assert and deassert within 60 s
R37_TEMP = ("   3 | 10/30/2026 | 12:49:59 PM GMT | Temperature DTS CPU1 | Upper Non-critical going high"
            " | Asserted | Reading 91 > Threshold 90 degrees C\n"
            "   4 | 10/30/2026 | 12:50:16 PM GMT | Temperature DTS CPU1 | Upper Non-critical going high"
            " | Deasserted | Reading 85 < Threshold 90 degrees C\n")
L05_FAN = ("   5 | 10/30/2026 | 12:49:44 PM GMT | Fan MB FAN1 TACH | Lower Critical going low"
           " | Asserted | Reading 0 < Threshold 500 RPM\n"
           "   6 | 10/30/2026 | 12:49:50 PM GMT | Fan MB FAN1 TACH | Lower Critical going low"
           " | Deasserted | Reading 4800 > Threshold 500 RPM\n")


def test_sel_threshold_transients_are_benign_unpaired_stays_red():
    r = _collect(Rig(sel=BENIGN + R37_TEMP + L05_FAN))
    assert r.verdict == "pass", r.detail["sel_nonbenign"]
    tr = {t["sensor"]: t["seconds"] for t in r.detail["sel_transients"]}
    assert tr == {"Temperature DTS CPU1": 17, "Fan MB FAN1 TACH": 6}
    assert "2 transient" in r.detail["headline"]
    # an Asserted with no Deasserted stays red; so does a pair more than 60 s apart
    lone = R37_TEMP.splitlines()[0]
    r = _collect(Rig(sel=BENIGN + lone + "\n" + L05_FAN))
    assert r.verdict == "fail" and r.detail["sel_nonbenign"] == [lone]
    late = R37_TEMP.replace("12:50:16 PM", "12:51:00 PM")
    r = _collect(Rig(sel=late))
    assert r.verdict == "fail" and len(r.detail["sel_nonbenign"]) == 2
    # a Deasserted of another sensor does not close it
    other = R37_TEMP.splitlines()[0] + "\n" + L05_FAN.splitlines()[1] + "\n"
    assert len(_collect(Rig(sel=other)).detail["sel_nonbenign"]) == 2
    # Non-recoverable (thermtrip class) never pairs away, nor a non-threshold event
    nr = R37_TEMP.replace("Upper Non-critical", "Upper Non-recoverable")
    assert len(_collect(Rig(sel=nr)).detail["sel_nonbenign"]) == 2
    crit = R37_TEMP.replace("Upper Non-critical going high", "Critical Interrupt")
    assert len(_collect(Rig(sel=crit)).detail["sel_nonbenign"]) == 2
