"""Lab-rack kind checks on a head's installed OS -- CHECK ONLY (applying the setup on a
reinstall is a later plan). Each read is one tagged shell script over the host ssh
(`run(script) -> (rc, out)`):

  bb-head         GRUB pci= args on /proc/cmdline (BB_PCI_ARGS: without them the BIOS
                  boxes an empty hotplug slot and the kernel faults on the JBOG hot-add),
                  nvidia-driver-580-server + cuda-toolkit-12-9 installed and held (Volta
                  left -open and CUDA 13), `nvidia-smi -L` = the Big Basin's expect.gpu,
                  BIOS MMIO High Size 1024G (a V100-32G BAR1 is 32 GiB; read from a fresh
                  SCELNX_64 /o dump -- a read, no reboot; the tool is fetched per run from
                  the share's AMI tpm_enabler bundle, as post's tpm_sce.py does -- an
                  installed OS has no /opt/flax/bin/SCELNX_64)
  lightning-head  NO pci= args (hpbussize=0x40 starves the PM8536's 16 ports and the mezz
                  NIC of buses: howlin 2026-10-28)
  others          nothing to check ("no kind setup")"""
import os
import re

from .result import RungResult

BB_PCI_ARGS = "pci=assign-busses,realloc,hpbussize=0x40,hpmmioprefsize=256G"
BB_PKGS = ("nvidia-driver-580-server", "cuda-toolkit-12-9")
MMIO_WANT = "1024G"
# Same bundle + URL as roles/apply_pxe_payloads/files/post/tpm_sce.py (SHARE_TGZ).
SCE_TGZ_URL = os.environ.get(
    "KIND_SCE_TGZ_URL",
    "http://bang/share/SXC-Lab_OCP-Updates/Linux_Flash_Utilities/tpm_enabler.v4.tgz")
_CMD = {
    "cmdline": "cat /proc/cmdline # cmdline",
    "pkgs": "dpkg -l " + " ".join(BB_PKGS) + " 2>/dev/null | grep -E '^(ii|hi) ' # pkgs",
    "gpus": "nvidia-smi -L 2>/dev/null # gpus",
    "mmio": (f"d=$(mktemp -d) && curl -fsSL '{SCE_TGZ_URL}' | tar -xz -C \"$d\" SCELNX_64 && "
             "\"$d/SCELNX_64\" /o /s \"$d/sce.txt\" >/dev/null 2>&1 && "
             "grep -A8 'MMIO High Size' \"$d/sce.txt\"; rc=$?; rm -rf \"$d\"; exit $rc # mmio"),
}


def _read(run, key) -> str:
    rc, out = run(_CMD[key])
    return out if rc == 0 else ""


def mmio_size(dump: str) -> str | None:
    """The selected option (the `*` one) of the SCE `MMIO High Size` question."""
    m = re.search(r"\*\s*\[[0-9A-Fa-f]+\]\s*(\S+)", dump or "")
    return m[1] if m else None


def check(kind: str, run, expect: dict) -> RungResult:
    rows, bad = [], []
    if kind == "bb-head":
        cmd = _read(run, "cmdline")
        rows.append(["/proc/cmdline", cmd.strip() or "not read"])
        if BB_PCI_ARGS not in cmd:
            bad.append(f"GRUB lacks {BB_PCI_ARGS}")
        pk = _read(run, "pkgs")
        held = {ln.split()[1]: ln.split()[0] for ln in pk.splitlines() if len(ln.split()) > 1}
        for p in BB_PKGS:
            rows.append([p, {"hi": "installed, held", "ii": "installed, NOT held"}.get(held.get(p), "missing")])
            if held.get(p) != "hi":
                bad.append(f"{p} " + ("not held" if held.get(p) == "ii" else "missing"))
        want = expect.get("gpu", 0)
        n = sum(1 for ln in _read(run, "gpus").splitlines() if ln.startswith("GPU "))
        rows.append(["nvidia-smi -L", f"{n} GPU"])
        if n != want:
            bad.append(f"{n}/{want} GPU in nvidia-smi")
        size = mmio_size(_read(run, "mmio"))
        rows.append(["MMIO High Size", size or "not read"])
        if size != MMIO_WANT:
            bad.append(f"MMIO High Size {size or 'not read'} (want {MMIO_WANT})")
    elif kind == "lightning-head":
        cmd = _read(run, "cmdline")
        rows.append(["/proc/cmdline", cmd.strip() or "not read"])
        if not cmd:
            bad.append("/proc/cmdline not read")
        elif "pci=" in cmd:
            bad.append("pci= args set (the PM8536 needs the BIOS bus numbering)")
    else:
        return RungResult("pass", {"headline": "no kind setup", "rows": rows})
    notes = ["check only: nothing is changed on the node (kind setup is applied on a "
             "reinstall, a later plan)"]
    if bad:
        return RungResult("fail", {"headline": bad[0] + (f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""),
                                   "rows": rows, "notes": notes, "problems": bad})
    return RungResult("pass", {"headline": "kind setup in place", "rows": rows, "notes": notes})
