"""An enclosure judged through its head's PCIe (lab racks: Big Basin, Lightning, Bryce
Canyon JBOD). The head's `lspci -vv` and `lsscsi -c` (the Post agent's inventory
captures) say what is behind it; the geometry's `expect` (the BoM) says what should be.

  bb-head         V100s (NVIDIA 3D controllers) behind the Big Basin PLX switches, each
                  at x16 (LnkSta); pass = all `expect.gpu` there, every one x16
  lightning-head  NVMe controllers on buses behind a PMC-Sierra/Microsemi PM8536 fanout
                  switch (its bridge Bus: secondary..subordinate); the head's own boot
                  NVMe is not counted; pass = `expect.nvme` of them
  bc-head         a SAS expander (an SES enclosure on the HBA, lsscsi Type Enclosure) and
                  `expect.disks` SAS disks (lsscsi Direct-Access, vendor not ATA)
Read only; nothing here touches the node."""
import re

from .result import RungResult

_DEV = re.compile(r"^([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])\s+([^:]+):\s*(.*)$")
_BUS = re.compile(r"Bus:\s*primary=([0-9a-f]{2}),\s*secondary=([0-9a-f]{2}),\s*subordinate=([0-9a-f]{2})")
_LNK = re.compile(r"LnkSta:\s*Speed [^,]*,\s*Width x(\d+)")
_SWITCH = re.compile(r"PMC-Sierra|Microsemi|PM85\d\d", re.I)
NAMES = {"bb-head": "Big Basin", "lightning-head": "Lightning", "bc-head": "Bryce Canyon JBOD",
         "gc-head": "Grand Canyon"}


def _blocks(text: str) -> list[dict]:
    out, cur = [], None
    for line in (text or "").splitlines():
        m = _DEV.match(line)
        if m:
            cur = {"bus": int(m[1], 16), "cls": m[4].strip(), "name": m[5], "width": None,
                   "range": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        b = _BUS.search(line)
        if b and cur["range"] is None:
            cur["range"] = (int(b[2], 16), int(b[3], 16))
        w = _LNK.search(line)
        if w and cur["width"] is None:
            cur["width"] = int(w[1])
    return out


def facts(lspci_vv: str, lsscsi_c: str) -> dict:
    bl = _blocks(lspci_vv)
    gpus = [b for b in bl if b["cls"] == "3D controller" and "NVIDIA" in b["name"]]
    plx = [b for b in bl if b["cls"] == "PCI bridge" and "PLX" in b["name"]]
    sw = [b for b in bl if b["cls"] == "PCI bridge" and _SWITCH.search(b["name"]) and b["range"]]
    # the fanout switch's upstream port: the one whose range holds the others'
    up = [b for b in sw if not any(o is not b and o["range"][0] <= b["bus"] <= o["range"][1]
                                   for o in sw)]
    behind = lambda bus: any(u["range"][0] <= bus <= u["range"][1] for u in up)
    nvme = [b for b in bl if b["cls"].startswith("Non-Volatile memory controller")]
    scsi = _scsi(lsscsi_c)
    return {"gpu": len(gpus), "gpu_x16": sum(1 for g in gpus if g["width"] == 16),
            "plx": len(plx), "pm8536": bool(sw),
            "nvme_behind_switch": sum(1 for n in nvme if behind(n["bus"])),
            "sas_hba": any(b["cls"] == "Serial Attached SCSI controller" for b in bl),
            "sas_expander": any(t == "Enclosure" for t, _ in scsi),
            "disks": sum(1 for t, v in scsi if t == "Direct-Access" and v != "ATA")}


def _scsi(text: str) -> list[tuple[str, str]]:
    """(Type, Vendor) per `lsscsi -c` / /proc/scsi/scsi device."""
    out, vendor = [], ""
    for line in (text or "").splitlines():
        m = re.search(r"Vendor:\s*(\S+)", line)
        if m:
            vendor = m[1]
        t = re.search(r"Type:\s*(\S+)", line)
        if t:
            out.append((t[1], vendor))
            vendor = ""
    return out


def judge(head_kind: str, expect: dict, f: dict) -> RungResult:
    rows = [[k, str(v)] for k, v in f.items()]
    notes = [f"read through the head's PCIe (lspci -vv, lsscsi -c): what the BoM says is "
             f"behind it ({', '.join(f'{k} {v}' for k, v in expect.items()) or 'nothing'})"]

    def out(ok, headline):
        return RungResult("pass" if ok else "fail",
                          {"headline": headline, "rows": rows, "notes": notes, "facts": f,
                           "expect": expect})
    if head_kind == "bb-head":
        want = expect.get("gpu", 0)
        if f["gpu"] == 0:
            return out(False, f"0/{want} V100 (no {NAMES[head_kind]} link)")
        return out(f["gpu"] == want and f["gpu_x16"] == f["gpu"],
                   f"{f['gpu']}/{want} V100 (x16 {f['gpu_x16']}/{f['gpu']})")
    if head_kind == "lightning-head":
        want = expect.get("nvme", 0)
        if not f["pm8536"]:
            return out(False, f"no PM8536 fanout switch (no {NAMES[head_kind]} link)")
        return out(f["nvme_behind_switch"] == want,
                   f"{f['nvme_behind_switch']}/{want} NVMe behind the PM8536")
    if head_kind in ("bc-head", "gc-head"):
        if not f["sas_hba"]:
            return out(False, "no SAS HBA")
        if not f["sas_expander"]:
            return out(False, "no SAS expander")
        if "disks" not in expect:       # no BoM count (Grand Canyon): the expander is the check
            return out(True, f"{f['disks']} SAS disks (no BoM count)")
        want = expect["disks"]
        return out(f["disks"] == want, f"{f['disks']}/{want} SAS disks")
    return RungResult("skip", {"headline": f"{head_kind} drives no enclosure", "na": True})
