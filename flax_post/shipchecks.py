# flax_post/shipchecks.py
"""Ship checks the engine judges from a run's inventory artifacts (spec
2026-10-29-post-qualify-serial-tpm-rungs). Pure: text in, verdict out.

Post only CHECKS what triage is supposed to have done; it never corrects. A
missing, blank or unreadable input is a fail, never a pass or a skip.

smbios-serial: the BIOS must have copied the FRU 0 ship serial into SMBIOS. The
rule is triage's tile rule (services/api/tileinv.py fru0_serials +
dmi_has_serial) so both tools agree, plus a placeholder filter: a FRU 0 serial
of "To be filled by O.E.M." must not "match" the same text in dmidecode.
"""
import re

_PLACEHOLDERS = {"to be filled by o.e.m.", "default string", "not specified", "n/a", "unknown", "-"}


def _blank(v) -> bool:
    v = (v or "").strip()
    return not v or v.lower() in _PLACEHOLDERS or set(v) == {"0"}


def fru0_serials(fru_text) -> list:
    """Every non-blank `* Serial : value` in the FRU ID 0 section of
    `ipmitool fru`, sorted."""
    out, in0 = set(), False
    for line in (fru_text or "").splitlines():
        if line.startswith("FRU Device Description"):
            in0 = "(ID 0)" in line
            continue
        if in0 and ":" in line:
            k, v = line.split(":", 1)
            if k.strip().endswith("Serial") and not _blank(v):
                out.add(v.strip())
    return sorted(out)


_DMI_BLOCKS = {"System Information": "System", "Chassis Information": "Chassis",
               "Base Board Information": "Base Board"}


def _dmi_serials(dmi_text) -> dict:
    """{System|Chassis|Base Board: the block's Serial Number, as read}."""
    out = {v: None for v in _DMI_BLOCKS.values()}
    block = None
    for line in (dmi_text or "").splitlines():
        if line and not line[0].isspace():
            block = _DMI_BLOCKS.get(line.strip())
            continue
        s = line.strip()
        if block and out[block] is None and s.startswith("Serial Number:"):
            out[block] = s.split(":", 1)[1].strip()
    return out


def _in_dmi(sn, dmi_text) -> bool:
    return re.search(r"(?<![\w.])%s(?![\w.])" % re.escape(sn), dmi_text) is not None


def smbios_serial(fru_text, dmi_text) -> dict:
    """{status: pass|fail, reason, fru0_serials, dmi_serials}."""
    sns = fru0_serials(fru_text)
    out = {"status": "fail", "reason": "", "fru0_serials": sns, "dmi_serials": _dmi_serials(dmi_text)}
    if not fru_text:
        out["reason"] = "no ipmitool-fru artifact"
    elif not dmi_text:
        out["reason"] = "no dmidecode artifact"
    elif not sns:
        out["reason"] = "FRU 0 has no serial"
    elif any(_in_dmi(sn, dmi_text) for sn in sns):
        out["status"] = "pass"
    else:
        out["reason"] = "SN %s in FRU 0, not in SMBIOS" % sns[0]
    return out


# tpm-state: the TPM is present, unowned and enabled -- tpm_clear.sh's goal
# (reaper e06f294), judged from the battery's read-only `tpmstate` capture:
# `device=present|absent`, then tpm_state.py's one line. The version comes from
# that line (`tpm=1.2`, else 2.0), never from the platform family. Disabled is
# tpm_clear.sh's definition: 1.2 enabled/active not 1; 2.0 storage or
# endorsement hierarchy off (phEnable=0 is normal). One stricter point: a 2.0
# line without the startup flags is unreadable here, enabled to tpm_clear.sh.
def tpm_state(text) -> dict:
    """{status: pass|fail, reason, device, version, fields, raw}."""
    out = {"status": "fail", "reason": "", "device": None, "version": None, "fields": {}, "raw": ""}
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        out["reason"] = "no tpmstate artifact"
        return out
    if lines[0].startswith("device="):
        out["device"] = lines.pop(0) == "device=present"
        if out["device"] is False:
            out["reason"] = "no TPM device"
            return out
    if not lines:
        out["reason"] = "unreadable: no state line"
        return out
    out["raw"] = lines[0]
    f = out["fields"] = dict(t.split("=", 1) for t in lines[0].split() if "=" in t)
    out["version"] = "1.2" if f.get("tpm") == "1.2" else "2.0"
    if f.get("owned") not in ("0", "1"):
        out["reason"] = "unreadable: %s" % (f.get("error") or lines[0])
    elif f["owned"] == "1":
        out["reason"] = "owned (not cleared)"
    elif out["version"] == "1.2":
        if f.get("enabled") != "1":
            out["reason"] = "TPM 1.2 disabled"
        elif f.get("active") != "1":
            out["reason"] = "TPM 1.2 inactive"
    elif "shEnable" not in f or "ehEnable" not in f:
        out["reason"] = "enable state unreadable"
    elif f["shEnable"] != "1":
        out["reason"] = "TPM 2.0 storage hierarchy off"
    elif f["ehEnable"] != "1":
        out["reason"] = "TPM 2.0 endorsement hierarchy off"
    if not out["reason"]:
        out["status"] = "pass"
    return out
