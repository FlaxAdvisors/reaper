"""flax_rungs.identity + flax_rack.switches.read_syseeprom: per-kind identity reads (detect only)."""
import pathlib
import re
import subprocess

import pytest

from flax_rack import switches
from flax_rungs import identity, remote
from flax_switch_sense.driver_sonic import SonicError

FIX = pathlib.Path(__file__).parent / "fixtures" / "rackpost"


def fx(name):
    return (FIX / name).read_text()


class Runner:
    """Answers by the remote command (last argv element) or the ipmitool args."""

    def __init__(self, by_cmd, rc=None):
        self.by_cmd, self.rc = by_cmd, rc or {}
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        key = " ".join(argv[argv.index("-E") + 1:]) if argv[0] == "ipmitool" else argv[-1]
        out = self.by_cmd.get(key, "")
        if callable(out):
            out = out(argv)
        return subprocess.CompletedProcess(argv, self.rc.get(key, 0), out, "")


# ------------------------------------------------------------------ TiogaPass FRU 0
def test_tp_fru_product_serial_and_raw_artifact():
    r = Runner({"fru print 0": fx("tp-fru0.txt")})
    res = identity.tp_fru("172.32.37.11", [("u", "p")], runner=r)
    assert res.verdict == "pass"
    assert res.detail["serial"] == "WTH48391DHM1A"
    assert res.detail["board_serial"] == "WTH18300T0ZA1"     # never the ship serial
    assert res.detail["cipher"] == 17 and res.artifacts["fru0.txt"] == fx("tp-fru0.txt")
    argv, kw = r.calls[0]
    assert "-P" not in argv and "p" not in argv and kw["env"]["IPMI_PASSWORD"] == "p"


def test_tp_fru_falls_back_to_cipher_3():
    r = Runner({"fru print 0": lambda a: fx("tp-fru0.txt") if a[a.index("-C") + 1] == "3" else ""},
               )
    res = identity.tp_fru("ip", [("u", "p")], runner=r)
    assert res.verdict == "pass" and res.detail["cipher"] == 3


def test_tp_fru_blank_serial_fails():
    text = re.sub(r"(Product Serial\s*:).*", r"\1 ", fx("tp-fru0.txt"))
    res = identity.tp_fru("ip", [("u", "p")], runner=Runner({"fru print 0": text}))
    assert res.verdict == "fail" and res.detail["serial"] is None


def test_tp_fru_no_answer_is_unmeasured():
    res = identity.tp_fru("ip", [("u", "p")], runner=Runner({}, rc={"fru print 0": 1}))
    assert res.verdict == "unmeasured" and "17/3" in res.detail["error"]
    assert "no IPMI answer" in res.detail["headline"]


# ------------------------------------------------------------------ BryceCanyon IOM
IOM = {identity.FRUID_IOM: fx("iom-fruid.txt"), identity.FWUTIL_BMC: fx("iom-fwutil-bmc.txt"),
       identity.FWUTIL_ALL: fx("iom-fwutil-all.txt"), identity.PAL_SKU: fx("iom-pal-sku.txt")}


def test_iom_board_serial_chosen_product_serial_na_ignored():
    r = Runner(IOM)
    res = identity.iom("172.26.0.70", "u", "p", runner=r)
    assert res.verdict == "pass", res.detail
    assert res.detail["serial"] == "J49110C3ZB1" and res.detail["serial_field"] == "Board Serial"
    assert res.detail["board_product"] == "BCIOM001"
    assert res.detail["phase"] == "unsupported"
    assert res.detail["headline"] == "SN J49110C3ZB1 · Side B · BMC fbttn-v2019.52.0"
    assert set(res.artifacts) == {"fruid-util-iom.txt", "fw-util-bmc.txt", "fw-util-all.txt",
                                  "check_pal_sku.txt"}


def test_iom_side_and_sku_read_though_the_script_exits_with_the_sku():
    # live: check_pal_sku.sh exits 33 on a Side B IOM (its status is the SKU)
    res = identity.iom("ip", "u", "p", runner=Runner(IOM, rc={identity.PAL_SKU: 33}))
    assert res.verdict == "pass"
    assert res.detail["side"] == "B" and res.detail["sku"] == "33 (00100001)"
    assert ["IOM Location", "Side B"] in res.detail["rows"]
    assert "check_pal_sku" not in res.detail["errors"]
    assert identity.parse_pal_sku(fx("iom-pal-sku.txt").replace("Side B", "Side A")) == \
        ("A", "33 (00100001)")


def test_iom_side_unread_is_noted_not_judged():
    res = identity.iom("ip", "u", "p", runner=Runner({**IOM, identity.PAL_SKU: ""},
                                                     rc={identity.PAL_SKU: 127}))
    assert res.verdict == "pass" and res.detail["side"] is None
    assert "check_pal_sku" in res.detail["errors"]
    assert identity.check_side(res, "A").verdict == "pass"


def test_iom_on_the_other_side_fails_wrong():
    res = identity.iom("ip", "u", "p", runner=Runner(IOM))
    assert identity.check_side(res, "B") is res
    bad = identity.check_side(res, "A")
    assert bad.verdict == "fail" and bad.detail["status"] == "wrong"
    assert bad.detail["headline"] == "wrong IOM: Side B, want Side A"
    assert bad.detail["serial"] == "J49110C3ZB1" and bad.artifacts == res.artifacts


def test_iom_versions_and_na_lines_are_not_present_not_errors():
    res = identity.iom("ip", "u", "p", runner=Runner(IOM))
    v, na = res.detail["versions"], res.detail["not_present"]
    assert v["BMC Version"] == "fbttn-v2019.52.0" and v["ROM Version"] == "fbttn-v2.0"
    assert v["TPM Version"] == "133.33"
    assert v["Fan Speed Controller Version"] == "fbttn-type5-mp-v3"
    assert v["Mellanox NIC firmware version"] == "14.20.1202"
    assert v["Expander Version"] == "0x1b0e0016"
    assert na["SCC IOC Version"] == "Server Power-off"
    assert na["BIOS Version"] == "server is empty" and na["MCU Version"] == "NA"
    assert not any(x.startswith("NA") for x in v.values())
    assert res.detail["errors"] == {}
    assert ["SCC IOC Version", "not present (Server Power-off)"] in res.detail["rows"]


def test_iom_uses_full_paths_and_ssh_options():
    r = Runner(IOM)
    identity.iom("ip", "u", "p", runner=r)
    cmds = [a[-1] for a, _ in r.calls]
    assert cmds == ["/usr/local/bin/fruid-util iom", "/usr/bin/fw-util bmc --version",
                    "/usr/bin/fw-util all --version", "sh /usr/local/bin/check_pal_sku.sh"]
    for argv, kw in r.calls:
        assert argv[:3] == ["sshpass", "-e", "ssh"] and "LogLevel=ERROR" in argv
        assert kw["env"]["SSHPASS"] == "p" and kw["input"] == ""


def test_iom_fw_util_failure_is_noted_identity_still_passes():
    res = identity.iom("ip", "u", "p", runner=Runner(IOM, rc={identity.FWUTIL_ALL: 1}))
    assert res.verdict == "pass" and "fw-util-all" in res.detail["errors"]


def test_iom_without_board_serial_fails():
    text = fx("iom-fruid.txt").replace("J49110C3ZB1", "N/A")
    res = identity.iom("ip", "u", "p", runner=Runner({**IOM, identity.FRUID_IOM: text}))
    assert res.verdict == "fail" and res.detail["serial"] is None


def test_iom_unreachable_is_unmeasured():
    res = identity.iom("ip", "u", "p", runner=Runner({}, rc={identity.FRUID_IOM: 255}))
    assert res.verdict == "unmeasured" and "rc=255" in res.detail["error"]
    assert res.detail["headline"] == "IOM ip did not answer ssh"


def test_iom_answering_but_failing_stays_fail():
    res = identity.iom("ip", "u", "p", runner=Runner({}, rc={identity.FRUID_IOM: 1}))
    assert res.verdict == "fail" and "rc=1" in res.detail["error"]


def test_wedge_bmc_unreachable_is_unmeasured():
    res = identity.wedge_bmc("ip", "u", "p", runner=Runner({}, rc={"weutil": 255}))
    assert res.verdict == "unmeasured" and "rc=255" in res.detail["error"]


# ------------------------------------------------------------------ Wedge BMC + SONiC
WEDGE = {"weutil": fx("wedge-weutil.txt"), "cat /etc/issue": "OpenBMC Release wedge100-v29\n\n",
         "/usr/local/bin/cpld_rev.sh": "6.101\n"}


def test_wedge_bmc_identity_and_versions():
    r = Runner(WEDGE)
    res = identity.wedge_bmc("172.26.0.61", "u", "p", runner=r)
    assert res.verdict == "pass"
    d = res.detail
    assert d["serial"] == "AI34020882" and d["local_mac"] == "B8:6A:97:19:C8:FD"
    assert d["ext_mac_base"] == "B8:6A:97:19:C8:FE" and d["asset_tag"] == "4620745"
    assert d["versions"] == {"OpenBMC": "wedge100-v29", "CPLD": "6.101"}
    assert d["phase"] == "unsupported"
    assert [a[-1] for a, _ in r.calls] == ["weutil", "cat /etc/issue",
                                           "/usr/local/bin/cpld_rev.sh"]


def _syseeprom(name):
    calls = []
    r = switches.read_syseeprom("172.26.0.60", "sonic", {"sonicuser": "u", "sonicpass": "p"},
                                runner=lambda cmd: calls.append(cmd) or fx(name))
    return r, calls


def test_wedge_serial_cross_checked_against_syseeprom():
    s, _ = _syseeprom("wedge-syseeprom.txt")
    res = identity.wedge_bmc("ip", "u", "p", syseeprom=s.detail, runner=Runner(WEDGE))
    assert res.verdict == "pass"
    assert ["SONiC syseeprom serial", "AI34020882 (matches)"] in res.detail["rows"]
    assert ["SONiC Base MAC", "b8:6a:97:19:c8:fe (matches from Extended MAC Base)"] \
        in res.detail["rows"]


def test_wedge_serial_mismatch_fails():
    s, _ = _syseeprom("wedge-syseeprom.txt")
    other = {**s.detail, "serial": "AI00000000"}
    res = identity.wedge_bmc("ip", "u", "p", syseeprom=other, runner=Runner(WEDGE))
    assert res.verdict == "fail" and "AI00000000" in res.detail["error"]
    assert ["SONiC syseeprom serial", "AI00000000 (DIFFERS)"] in res.detail["rows"]


def test_wedge_syseeprom_parse():
    s, r = _syseeprom("wedge-syseeprom.txt")
    assert s.verdict == "pass"
    assert s.detail["serial"] == "AI34020882" and s.detail["service_tag"] == "4620745"
    assert s.detail["base_mac"] == "b8:6a:97:19:c8:fe"
    assert s.detail["platform"] == "x86_64-accton_wedge100s_32x"
    assert s.detail["fields"]["CRC-32"] == "0x711189C1"


def test_dell_syseeprom_serial_and_service_tag():
    s, _ = _syseeprom("dell-syseeprom.txt")
    assert s.verdict == "pass"
    assert s.detail["serial"] == "TW05WPCHDNT0008R0058" and s.detail["service_tag"] == "7PN8PK2"
    assert s.detail["fields"]["Vendor Extension"] == "0x00 0x00 0x02 0xA2"
    assert ["Service Tag", "7PN8PK2"] in s.detail["rows"]
    assert s.detail["headline"] == "SN TW05WPCHDNT0008R0058"


def test_syseeprom_runs_under_sudo_with_empty_stdin():
    s, calls = _syseeprom("dell-syseeprom.txt")
    assert calls == ["sudo -n show platform syseeprom </dev/null"]


def test_syseeprom_failure_fails():
    def boom(cmd):
        raise SonicError("h: ssh exited 1", 1)
    res = switches.read_syseeprom("h", "sonic", {}, runner=boom)
    assert res.verdict == "fail" and res.detail["headline"]
