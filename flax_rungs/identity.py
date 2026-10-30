"""Per-kind identity + firmware version reads (read-only).

- TiogaPass BMC: `ipmitool fru print 0` Product Serial (lanplus, cipher 17 then 3).
- BryceCanyon IOM (Facebook OpenBMC fbttn): `fruid-util iom` Board Serial (its Product
  Serial is N/A), `fw-util bmc --version` and `fw-util all --version`, and its side
  (`check_pal_sku.sh`: `IOM Location: Side A|B`, `Platform SKU: 17 (00010001)`; the
  script exits with the SKU as its status, so its rc is not an error).
- Wedge100S BMC (Facebook OpenBMC wedge100): `weutil` Product Serial Number, `/etc/issue`,
  `cpld_rev.sh`; optionally cross-checked against the SONiC side's syseeprom.

Facebook OpenBMC's non-login PATH lacks /usr/local/bin, so those commands are full paths
(`weutil` and `cat` resolve on it: the RRORD-101 inventory ran them bare over raw ssh).
No manifest exists for those BMCs: their firmware phase is `unsupported` (report only).
Every result carries Post's stepinfo shape (headline/rows/notes); artifacts = raw output."""
import subprocess

from .bmc import CIPHERS, _ipmi
from .remote import ssh_cmd, unreachable
from .result import UNMEASURED, RungResult

FRUID_IOM = "/usr/local/bin/fruid-util iom"
FWUTIL_BMC = "/usr/bin/fw-util bmc --version"
FWUTIL_ALL = "/usr/bin/fw-util all --version"
WEUTIL = "weutil"
ISSUE = "cat /etc/issue"
CPLD_REV = "/usr/local/bin/cpld_rev.sh"
PAL_SKU = "sh /usr/local/bin/check_pal_sku.sh"

_BLANK = {"", "n/a", "na", "none", "unknown"}


def parse_fields(text: str) -> dict[str, str]:
    """`Field : value` / `Field: value` lines -> {field: value}; the first non-empty value
    of a key wins. Ruler rows (`----- : -----`) and section titles with no value drop."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if not sep or not k or not v or set(k) <= {"-"} or k in out:
            continue
        out[k] = v
    return out


def _value(fields: dict, key: str) -> str | None:
    v = (fields.get(key) or "").strip()
    return None if v.lower() in _BLANK else v


def parse_versions(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """fw-util `<Component> Version: <v>` lines -> (present {component: version},
    not_present {component: reason}). `NA` / `NA (Server Power-off)` mean the part is
    absent or unpowered (an IOM whose server side is off): not present, not an error."""
    present, absent = {}, {}
    for k, v in parse_fields(text).items():
        if v == "NA" or v.startswith("NA ("):
            absent[k] = v[4:-1] if v.startswith("NA (") and v.endswith(")") else "NA"
        else:
            present[k] = v
    return present, absent


def parse_pal_sku(text: str) -> tuple[str | None, str | None]:
    """check_pal_sku.sh -> (side "A"|"B" | None, platform SKU text | None)."""
    f = parse_fields(text)
    loc = (f.get("IOM Location") or "").strip()
    side = loc[5:].strip() if loc.lower().startswith("side ") else None
    return (side if side in ("A", "B") else None), (f.get("Platform SKU") or None)


def check_side(r: RungResult, want: str | None) -> RungResult:
    """An IOM serial result judged against the side the geometry puts on its link: a
    passing read that reports the other side fails `wrong` (the two IOM cables are
    swapped, or the geometry is). An unread side or no expectation is left as is."""
    side = r.detail.get("side")
    if r.verdict != "pass" or not want or not side or side == want:
        return r
    return RungResult("fail", {**r.detail, "status": "wrong", "want_side": want,
                               "error": f"IOM reports Side {side}, geometry wants Side {want} "
                                        "on this link (IOM cables swapped?)",
                               "headline": f"wrong IOM: Side {side}, want Side {want}"},
                      r.artifacts)


def _fail(headline: str, error: str, arts=None, **extra) -> RungResult:
    return RungResult("fail", {"error": error, "headline": headline, "rows": [],
                               "notes": [], **extra}, arts or {})


def _no_answer(headline: str, error: str, arts=None) -> RungResult:
    """The device never answered (ssh unreachable / no IPMI session): `unmeasured`,
    nothing judged. `error` is kept for the callers that test its text."""
    return RungResult(UNMEASURED, {"error": error, "headline": headline, "reason": headline,
                                   "rows": [], "notes": []}, arts or {})


# ---------------------------------------------------------------- TiogaPass FRU 0
def tp_fru(bmc_ip: str, creds: list[tuple[str, str]], *, runner=subprocess.run) -> RungResult:
    """Out-of-band FRU 0 (`ipmitool fru print 0`), first credential x cipher that
    answers. Product Serial is the TiogaPass ship serial; the raw text is the `fru0`
    artifact (callers that need Post's family-aware reading run it through
    observe.fru.read_baseboard)."""
    last = "no BMC credentials"
    for user, pw in creds:
        for cipher in CIPHERS:
            p = _ipmi(bmc_ip, user, pw, ["fru", "print", "0"], cipher=cipher, runner=runner)
            if p is None:
                last = "ipmitool timed out"
                continue
            if p.returncode != 0 or ":" not in (p.stdout or ""):
                last = (p.stderr or p.stdout or f"rc={p.returncode}").strip()[-300:]
                continue
            return _tp_result(p.stdout, user, cipher)
    # no credential x cipher got FRU 0 text: no IPMI session, nothing judged
    why = f"{last} (ciphers {'/'.join(map(str, CIPHERS))})"
    return _no_answer(f"FRU 0 not read from {bmc_ip}: no IPMI answer", why)


def _tp_result(text: str, user: str, cipher: int) -> RungResult:
    f = parse_fields(text)
    arts = {"fru0.txt": text}
    serial = _value(f, "Product Serial")
    detail = {"serial": serial, "serial_field": "Product Serial",
              "product_name": f.get("Product Name"), "board_product": f.get("Board Product"),
              "board_serial": f.get("Board Serial"), "user": user, "cipher": cipher,
              "rows": [["Product Serial", serial or "empty"],
                       ["Product Name", f.get("Product Name") or "-"],
                       ["Board Serial", f.get("Board Serial") or "-"],
                       ["cipher", str(cipher)]],
              "notes": ["read out of band from the BMC: `ipmitool fru print 0` (FRU ID 0 only)"]}
    if "Device not present" in text:
        return RungResult("fail", {**detail, "error": "FRU 0 device not present",
                                   "headline": "FRU 0 not present"}, arts)
    if not serial:
        return RungResult("fail", {**detail, "error": "FRU 0 Product Serial is empty",
                                   "headline": "FRU 0 has no Product Serial"}, arts)
    return RungResult("pass", {**detail, "headline": f"SN {serial}"}, arts)


# ---------------------------------------------------------------- BryceCanyon IOM
def iom(bmc_ip: str, user: str, pw: str, *, runner=subprocess.run) -> RungResult:
    """IOM identity (Board Serial) + its firmware versions (report only: phase
    `unsupported`, nothing to compare against)."""
    rc, out, err = ssh_cmd(bmc_ip, user, pw, FRUID_IOM, runner=runner)
    arts = {"fruid-util-iom.txt": out + err}
    if rc != 0:
        why = f"fruid-util: rc={rc} {(err or out).strip()[-300:]}"
        if unreachable(rc):
            return _no_answer(f"IOM {bmc_ip} did not answer ssh", why, arts)
        return _fail(f"IOM {bmc_ip} not read", why, arts)
    f = parse_fields(out)
    serial = _value(f, "Board Serial")
    versions, absent, errors = {}, {}, {}
    for name, cmd in (("fw-util-bmc", FWUTIL_BMC), ("fw-util-all", FWUTIL_ALL)):
        vrc, vout, verr = ssh_cmd(bmc_ip, user, pw, cmd, runner=runner)
        arts[f"{name}.txt"] = vout + verr
        if vrc != 0:
            errors[name] = f"rc={vrc} {(verr or vout).strip()[-200:]}"
            continue
        p, a = parse_versions(vout)
        versions.update(p)
        absent.update(a)
    src, sout, serr = ssh_cmd(bmc_ip, user, pw, PAL_SKU, runner=runner)
    arts["check_pal_sku.txt"] = sout + serr
    side, sku = parse_pal_sku(sout)
    if side is None:
        errors["check_pal_sku"] = f"rc={src} {(serr or sout).strip()[-200:] or 'no IOM Location'}"
    rows = [["Board Serial", serial or "empty"],
            ["Board Product", f.get("Board Product") or "-"],
            ["Board Part Number", f.get("Board Part Number") or "-"],
            ["IOM Location", f"Side {side}" if side else "not read"],
            ["Platform SKU", sku or "-"]]
    rows += [[k, v] for k, v in versions.items()]
    rows += [[k, f"not present ({why})"] for k, why in absent.items()]
    notes = ["identity is the IOM's Board Serial (its Product Serial is N/A)",
             "no firmware manifest exists for Facebook OpenBMC IOMs: versions are report only"]
    notes += [f"{k} failed: {v}" for k, v in errors.items()]
    detail = {"serial": serial, "serial_field": "Board Serial",
              "board_product": f.get("Board Product"), "part_number": f.get("Board Part Number"),
              "versions": versions, "not_present": absent, "errors": errors,
              "side": side, "sku": sku,
              "phase": "unsupported", "rows": rows, "notes": notes}
    if not serial:
        return RungResult("fail", {**detail, "error": "fruid-util iom has no Board Serial",
                                   "headline": "IOM has no Board Serial"}, arts)
    bmc_v = versions.get("BMC Version")
    return RungResult("pass", {**detail, "headline": f"SN {serial}"
                               + (f" · Side {side}" if side else "")
                               + (f" · BMC {bmc_v}" if bmc_v else "")}, arts)


# ---------------------------------------------------------------- Wedge100S BMC
def wedge_bmc(bmc_ip: str, user: str, pw: str, *, syseeprom: dict | None = None,
              runner=subprocess.run) -> RungResult:
    """Wedge identity from `weutil` (Product Serial Number; Local MAC = the BMC,
    Extended MAC Base = the SONiC side) + OpenBMC release + CPLD revision (report only).
    `syseeprom`, a sonic.read_syseeprom detail, adds the cross-check rows: a serial
    that differs from the SONiC side's fails (the two endpoints are not one switch)."""
    rc, out, err = ssh_cmd(bmc_ip, user, pw, WEUTIL, runner=runner)
    arts = {"weutil.txt": out + err}
    if rc != 0:
        why = f"weutil: rc={rc} {(err or out).strip()[-300:]}"
        if unreachable(rc):
            return _no_answer(f"wedge BMC {bmc_ip} did not answer ssh", why, arts)
        return _fail(f"wedge BMC {bmc_ip} not read", why, arts)
    f = parse_fields(out)
    serial = _value(f, "Product Serial Number")
    versions, errors = {}, {}
    for name, cmd in (("issue", ISSUE), ("cpld", CPLD_REV)):
        vrc, vout, verr = ssh_cmd(bmc_ip, user, pw, cmd, runner=runner)
        arts[f"{name}.txt"] = vout + verr
        if vrc != 0:
            errors[name] = f"rc={vrc} {(verr or vout).strip()[-200:]}"
            continue
        line = next((x.strip() for x in vout.splitlines() if x.strip()), "")
        if name == "issue":
            # "OpenBMC Release wedge100-v29" (any trailing getty escapes dropped)
            line = line.split("\\")[0].strip()
            versions["OpenBMC"] = line.removeprefix("OpenBMC Release").strip() or line
        else:
            versions["CPLD"] = line
    rows = [["Product Serial Number", serial or "empty"],
            ["Product Name", f.get("Product Name") or "-"],
            ["Local MAC (BMC)", f.get("Local MAC") or "-"],
            ["Extended MAC Base", f.get("Extended MAC Base") or "-"],
            ["Product Asset Tag", f.get("Product Asset Tag") or "-"]]
    rows += [[k, v] for k, v in versions.items()]
    notes = ["no firmware manifest exists for Facebook OpenBMC wedges: versions are report only"]
    notes += [f"{k} failed: {v}" for k, v in errors.items()]
    detail = {"serial": serial, "serial_field": "Product Serial Number",
              "product_name": f.get("Product Name"), "local_mac": f.get("Local MAC"),
              "ext_mac_base": f.get("Extended MAC Base"),
              "asset_tag": f.get("Product Asset Tag"), "versions": versions,
              "errors": errors, "phase": "unsupported", "rows": rows, "notes": notes}
    mismatch = None
    if syseeprom is not None:
        s_serial = syseeprom.get("serial")
        same = bool(serial) and s_serial == serial
        rows.append(["SONiC syseeprom serial",
                     f"{s_serial or 'none'} ({'matches' if same else 'DIFFERS'})"])
        s_mac = (syseeprom.get("base_mac") or "").lower()
        if s_mac:
            rows.append(["SONiC Base MAC", f"{s_mac} ("
                         f"{'matches' if s_mac == (f.get('Extended MAC Base') or '').lower() else 'differs'}"
                         " from Extended MAC Base)"])
        detail["syseeprom_serial"] = s_serial
        if not same:
            mismatch = f"weutil serial {serial or 'empty'} != SONiC syseeprom serial {s_serial or 'none'}"
    if not serial:
        return RungResult("fail", {**detail, "error": "weutil has no Product Serial Number",
                                   "headline": "wedge has no Product Serial Number"}, arts)
    if mismatch:
        return RungResult("fail", {**detail, "error": mismatch,
                                   "headline": "wedge BMC and SONiC serials differ"}, arts)
    return RungResult("pass", {**detail, "headline": f"SN {serial}"
                               + (f" · {versions['OpenBMC']}" if "OpenBMC" in versions else "")},
                      arts)
