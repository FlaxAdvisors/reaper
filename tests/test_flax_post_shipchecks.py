# tests/test_flax_post_shipchecks.py
"""shipchecks: the engine's smbios-serial and tpm-state judges (spec
2026-10-29-post-qualify-serial-tpm-rungs)."""
from flax_post import shipchecks


def _fru(id0, id1="Product Serial        : OTHERBOARD1\n"):
    return ("FRU Device Description : Builtin FRU Device (ID 0)\n" + id0 + "\n"
            "FRU Device Description : Mezz (ID 1)\n" + id1)


FRU = _fru(" Chassis Type          : Rack Mount Chassis\n"
           " Chassis Serial        : C.M.B60.00X1C.001109201901IW\n"
           " Board Serial          : WTHBOARD0001\n"
           " Product Serial        : WTH49152YTM1A\n")
FRU_OEM = _fru(" Chassis Serial        : To be filled by O.E.M.\n"
               " Product Serial        : To be filled by O.E.M.\n"
               " Board Serial          : 00000000\n")
FRU_BLANK = _fru(" Chassis Serial        : \n Product Serial        :\n")
FRU_SERIAL_ONLY_IN_ID1 = _fru(" Product Name          : Tioga Pass\n")
FRU_WISTRON = _fru(" Product Serial        : WTH49152YTM1A\n")


def _dmi(system, chassis="To be filled by O.E.M.", board="To be filled by O.E.M."):
    return ("Handle 0x0001, DMI type 1, 27 bytes\nSystem Information\n"
            "\tManufacturer: Wiwynn\n\tSerial Number: %s\n\n"
            "Handle 0x0002, DMI type 2, 15 bytes\nBase Board Information\n"
            "\tSerial Number: %s\n\n"
            "Handle 0x0003, DMI type 3, 22 bytes\nChassis Information\n"
            "\tSerial Number: %s\n" % (system, board, chassis))


DMI_GOOD = _dmi("WTH49152YTM1A")
DMI_OEM = _dmi("To be filled by O.E.M.")
DMI_LONGER = _dmi("WTH49152YTM1AX")
DMI_HAS_ID1_SERIAL = _dmi("OTHERBOARD1")
DMI_PRODUCT_ONLY = _dmi("WTH49152YTM1A")


def test_serial_in_smbios_passes():
    r = shipchecks.smbios_serial(FRU, DMI_GOOD)
    assert r["status"] == "pass" and r["reason"] == ""


def test_serial_not_in_smbios_fails():
    r = shipchecks.smbios_serial(FRU_WISTRON, DMI_OEM)
    assert r["status"] == "fail" and r["reason"] == "SN WTH49152YTM1A in FRU 0, not in SMBIOS"
    assert r["dmi_serials"]["System"] == "To be filled by O.E.M."


def test_dmi_serials_read_per_block():
    r = shipchecks.smbios_serial(FRU, _dmi("SYS1", chassis="CH1", board="BB1"))
    assert r["dmi_serials"] == {"System": "SYS1", "Chassis": "CH1", "Base Board": "BB1"}


def test_fru0_serials_all_serial_keys_sorted():
    assert shipchecks.fru0_serials(FRU) == ["C.M.B60.00X1C.001109201901IW", "WTH49152YTM1A", "WTHBOARD0001"]


def test_only_fru_id0_counts():
    r = shipchecks.smbios_serial(FRU_SERIAL_ONLY_IN_ID1, DMI_HAS_ID1_SERIAL)
    assert r["status"] == "fail" and r["reason"] == "FRU 0 has no serial"


def test_whole_token_only():
    assert shipchecks.smbios_serial(FRU_WISTRON, DMI_LONGER)["status"] == "fail"


def test_placeholder_fru_serial_never_matches():
    r = shipchecks.smbios_serial(FRU_OEM, DMI_OEM)
    assert r["status"] == "fail" and r["reason"] == "FRU 0 has no serial"


def test_blank_fru0_fails():
    assert shipchecks.smbios_serial(FRU_BLANK, DMI_GOOD)["reason"] == "FRU 0 has no serial"


def test_missing_artifacts_fail():
    assert shipchecks.smbios_serial(None, DMI_GOOD)["reason"] == "no ipmitool-fru artifact"
    assert shipchecks.smbios_serial(FRU, None)["reason"] == "no dmidecode artifact"
    assert shipchecks.smbios_serial("", DMI_GOOD)["status"] == "fail"


def test_any_fru0_serial_may_match():
    # Wistron: Chassis Serial is a lot number, never in SMBIOS; Product Serial is
    assert shipchecks.smbios_serial(FRU, DMI_PRODUCT_ONLY)["status"] == "pass"


# --- tpm-state ---------------------------------------------------------------
_T2 = ("owned=%s perm=0x%s ownerAuthSet=%s endorsementAuthSet=0 lockoutAuthSet=0 disableClear=0 "
       "persistent=%s")
_START = " startup=0x80000006 shEnable=%s ehEnable=%s phEnable=0"


def _t2(owned=0, sh=1, eh=1, startup=True):
    line = _T2 % (owned, "00000007" if owned else "00000000", owned, 2 if owned else 0)
    if startup:
        line += _START % (sh, eh)
    return "device=present\n" + line + "\n"


def _t12(owned=0, enabled=1, active=1):
    return "device=present\nowned=%s tpm=1.2 enabled=%s active=%s\n" % (owned, enabled, active)


def _fail(text, reason):
    r = shipchecks.tpm_state(text)
    assert (r["status"], r["reason"]) == ("fail", reason)
    return r


def test_tpm2_clean_passes():
    r = shipchecks.tpm_state(_t2())
    assert (r["status"], r["reason"], r["version"], r["device"]) == ("pass", "", "2.0", True)
    assert r["fields"]["shEnable"] == "1" and r["raw"].startswith("owned=0 perm=")


def test_tpm2_owned_fails():
    _fail(_t2(owned=1), "owned (not cleared)")


def test_owned_beats_disabled():
    _fail(_t2(owned=1, sh=0), "owned (not cleared)")


def test_tpm2_sh_off_fails():
    _fail(_t2(sh=0), "TPM 2.0 storage hierarchy off")


def test_tpm2_eh_off_fails():
    _fail(_t2(eh=0), "TPM 2.0 endorsement hierarchy off")


def test_tpm2_without_startup_fields_fails():
    _fail(_t2(startup=False), "enable state unreadable")


def test_tpm12_clean_passes():
    r = shipchecks.tpm_state(_t12())
    assert (r["status"], r["version"]) == ("pass", "1.2")


def test_tpm12_disabled_fails():
    _fail(_t12(enabled=0), "TPM 1.2 disabled")


def test_tpm12_inactive_fails():
    _fail(_t12(active=0), "TPM 1.2 inactive")


def test_no_device_fails():
    r = _fail("device=absent\n", "no TPM device")
    assert r["device"] is False


def test_unreadable_fails():
    _fail("device=present\nowned=? error=TPM_rc=0x101\n", "unreadable: TPM_rc=0x101")
    _fail("device=present\nowned=? tpm=1.2 error=no-owned-attribute\n", "unreadable: no-owned-attribute")


def test_missing_artifact_fails():
    _fail(None, "no tpmstate artifact")
    _fail("", "no tpmstate artifact")


def test_present_without_state_line_is_unreadable():
    _fail("device=present\n", "unreadable: no state line")


def test_version_from_line_not_platform():
    # the judge takes no platform input at all: a 1.2 line is judged as 1.2
    assert shipchecks.tpm_state(_t12())["version"] == "1.2"
    assert shipchecks.tpm_state(_t2())["version"] == "2.0"


def test_tpm12_unknown_enable_reason_is_unreadable_not_disabled():
    # tpm_clear.sh only fixes enabled=0; "disabled" would send the operator to
    # triage for a TPM triage sees nothing wrong with
    _fail("device=present\nowned=0 tpm=1.2 enabled=? active=1\n", "TPM 1.2 enable state unreadable")
    _fail("device=present\nowned=0 tpm=1.2 enabled=1 active=?\n", "TPM 1.2 enable state unreadable")


def test_no_device_line_never_passes():
    r = shipchecks.tpm_state("owned=0 tpm=1.2 enabled=1 active=1\n")
    assert (r["status"], r["reason"]) == ("fail", "unreadable: no device line")


def test_evidence_text_records_checker_and_state():
    c = dict(shipchecks.tpm_state(_t2(sh=0)), checker="flax-post 9.9.9")
    t = shipchecks.evidence_text("tpm-state", c)
    for want in ("check: tpm-state", "checked by: flax-post 9.9.9", "status: fail",
                 "reason: TPM 2.0 storage hierarchy off", "device: present", "version: 2.0",
                 "state line: owned=0 perm="):
        assert want in t
    s = shipchecks.evidence_text("smbios-serial", dict(shipchecks.smbios_serial(FRU, DMI_GOOD), checker="flax-post 9.9.9"))
    for want in ("status: pass", "FRU 0 serials: C.M.B60.00X1C.001109201901IW, WTH49152YTM1A, WTHBOARD0001",
                 "SMBIOS System: WTH49152YTM1A", "SMBIOS Chassis: To be filled by O.E.M."):
        assert want in s
