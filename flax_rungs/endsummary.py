"""Done's end-summary of one node (donum 83 postinstall summary, end-summary-collect).

Two halves, each saved as text artifacts:
  out of band  ipmitool lanplus `sdr elist` + `sel elist` on the credential x cipher
               pair whose `chassis power status` answers (bmc._session: 17, then 3);
               the password rides in IPMI_PASSWORD (-E), never in argv.
  in band      one ssh script on the installed OS (hostssh.run, `sudo -n`): `ip -br link;
               ip -br addr`, `lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE`, `efibootmgr -v`,
               `dmidecode -t 17`. host_ip None (no installed OS) skips it. Each section
               runs regardless of the others and reports its own exit code: a failed
               one is a row + detail["inband_errors"][key], its text (the error) still
               saved, the others kept.

The verdict is the SEL's: `fail` on any line no BENIGN_SEL pattern matches, `unmeasured`
when the SEL was not read (no IPMI session, or `sel elist` failed: SEL not read is not
SEL clean), else `pass`. The in-band half never decides it: a host that does not answer
is a row (`inband: false`) beside a SEL that was read. Read-only: nothing here
power-controls the node or clears its SEL.
"""
import re
import subprocess

from . import bmc, hostssh
from .result import RungResult

# donum 83's benign SEL patterns, verbatim (substring match against an elist line)
BENIGN_SEL = ("Inlet Temp | Upper Critical", "C1 Remote Temp | Upper Critical",
              "Unknown Chassis Pwr Sts")
SEL_TIMEOUT_S = 120         # a flooded SEL takes far longer than a power status
_MARK = "===flax-es "
_INBAND = (("net", "net.txt", "ip -br link; ip -br addr"),
           ("disks", "disks.txt", "lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE"),
           ("bootorder", "bootorder.txt", "efibootmgr -v"),
           ("dimms", "dimms.txt", "dmidecode -t 17"))


_RC = "===flax-es-rc "


def _script() -> str:
    """Every section runs whatever the one before it did; each ends with its own exit
    code, and the script exits 0 (ssh's rc is then ssh's/sudo's, never a section's)."""
    return "".join(f"echo '{_MARK}{key}==='\n{{ {cmd}; }} 2>&1\necho \"{_RC}{key} $?\"\n"
                   for key, _n, cmd in _INBAND) + "exit 0\n"


def _split(out: str) -> tuple[dict[str, str], dict[str, int]]:
    """(section key -> its text, section key -> its exit code) from the marked output."""
    secs, rcs, cur = {}, {}, None
    for ln in out.splitlines(keepends=True):
        if ln.startswith(_RC):
            k, _sp, rc = ln[len(_RC):].strip().partition(" ")
            rcs[k] = int(rc) if rc.isdigit() else -1
            cur = None
        elif ln.startswith(_MARK) and ln.rstrip().endswith("==="):
            cur = ln.strip()[len(_MARK):-3]
            secs[cur] = ""
        elif cur is not None:
            secs[cur] += ln
    return secs, rcs


_NET_BOOT = re.compile(r"\b(?:PXE|IPv4|IPv6|IP4|IP6|HTTP|Network)\b|MAC\(", re.I)


def first_boot(bootorder_text: str) -> tuple[str | None, str | None, bool]:
    """(BootOrder's first entry id, its efibootmgr -v line, is it a network/PXE entry)
    from `efibootmgr -v` text; (None, None, False) without a BootOrder line."""
    m = re.search(r"^BootOrder:\s*([0-9A-Fa-f]{4})", bootorder_text or "", re.M)
    if not m:
        return None, None, False
    bid = m.group(1).upper()
    line = next((ln.strip() for ln in bootorder_text.splitlines()
                 if re.match(rf"Boot{bid}\b\*?", ln.strip(), re.I)), None)
    return bid, line, bool(line and _NET_BOOT.search(line))


def nonbenign(sel_text: str) -> list[str]:
    """The SEL entries (elist lines, `|`-separated) no BENIGN_SEL pattern matches."""
    return [ln.rstrip() for ln in (sel_text or "").splitlines()
            if "|" in ln and not any(p in ln for p in BENIGN_SEL)]


def _inband(host_ip, user, pw, runner, arts, rows, errors) -> bool:
    if not host_ip:
        rows.append(["in-band", "skipped: no installed OS to reach"])
        return False
    rc, out, err = hostssh.run(host_ip, user, pw, _script(), timeout=120, runner=runner)
    secs, rcs = _split(out)
    if not secs:
        rows.append(["in-band", f"ssh {host_ip} rc={rc}: {hostssh.tail('', err, 160)}".strip()])
        return False
    bad = {}
    for key, name, cmd in _INBAND:
        if key in secs:                 # kept even when its command failed: the error text
            arts[name] = secs[key]
        if key not in rcs:
            bad[key] = f"{cmd}: not run (output ends before it)"
        elif rcs[key] != 0:
            bad[key] = f"{cmd}: rc={rcs[key]}: {hostssh.tail(secs.get(key, ''), '', 120)}"
    ok = [n for k, n, _c in _INBAND if n in arts and k not in bad]
    rows.append(["in-band", f"{host_ip}: {', '.join(ok) or 'no section read'}"
                 + (f" (ssh rc={rc})" if rc else "")])
    rows += [[f"in-band {k}", v] for k, v in bad.items()]
    errors.update(bad)
    return True


def collect(host_ip: str | None, bmc_ip: str, *, host_user: str, host_pw: str,
            bmc_creds: list[tuple[str, str]], runner=subprocess.run) -> RungResult:
    arts: dict[str, str] = {}
    oob_rows: list[list[str]] = []
    host_rows: list[list[str]] = []
    detail = {"inband": False, "oob": False, "sel_nonbenign": [], "user": None,
              "cipher": None, "inband_errors": {}}
    detail["inband"] = _inband(host_ip, host_user, host_pw, runner, arts, host_rows,
                               detail["inband_errors"])

    why = None
    s = bmc._session(bmc_ip, bmc_creds, runner=runner)
    if s is None:
        why = f"no BMC session at {bmc_ip} (every credential x cipher refused)"
        oob_rows.append(["BMC", why])
    else:
        user, pw, cipher, _power = s
        detail.update(oob=True, user=user, cipher=cipher)
        oob_rows.append(["BMC", f"{bmc_ip} as {user}, cipher {cipher}"])
        for args, name in ((["sdr", "elist"], "sdr-elist.txt"),
                           (["sel", "elist"], "sel-elist.txt")):
            p = bmc._ipmi(bmc_ip, user, pw, args, cipher=cipher, timeout=SEL_TIMEOUT_S,
                          runner=runner)
            if p is not None and p.returncode == 0:
                arts[name] = p.stdout or ""
                oob_rows.append([" ".join(args), "read"])
            else:
                got = ("timeout" if p is None
                       else (p.stderr or p.stdout or f"rc={p.returncode}").strip()[-160:])
                oob_rows.append([" ".join(args), f"not read: {got}"])
                why = f"{' '.join(args)} failed: {got}"
    sel = arts.get("sel-elist.txt")
    detail["rows"] = rows = oob_rows + host_rows
    if sel is None:
        head = f"SEL not read: {why}"
        return RungResult("unmeasured", {**detail, "headline": head, "reason": head}, arts)
    bad = nonbenign(sel)
    entries = sum("|" in ln for ln in sel.splitlines())
    detail["sel_nonbenign"] = bad
    rows.append(["SEL", f"{entries} entr{'y' if entries == 1 else 'ies'}, "
                        f"{len(bad)} non-benign"])
    host = "" if detail["inband"] else "; in-band not read"
    if bad:
        head = f"SEL: {len(bad)} non-benign: {bad[0]}"
        return RungResult("fail", {**detail, "headline": head, "error": head}, arts)
    return RungResult("pass", {**detail, "headline": f"SEL clean ({entries} benign){host}"},
                      arts)
