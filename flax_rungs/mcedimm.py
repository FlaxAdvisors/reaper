"""Name the DIMM behind rasdaemon's memory errors, so a stress fail says which stick.

dimmerr reads EDAC's per-DIMM counters, but Ubuntu's kernel (CONFIG_RAS_CEC=y) parks
corrected memory errors in its correctable-error collector, which hands one on to EDAC
only after ~1023 on the same page; until then skx_edac skips it and every count stays 0
(LVQ et7b4 2026-11-10: 2 CEs, dimmerr all zero). rasdaemon still records the raw MCE.
The images boot with ras=cec_disable now, but a box booted without it (or a kernel with
no EDAC driver) still only has the MCE, so the slot is decoded from the MCE itself.

An event is located as dimmerr's dmidecode key _Node{S}_Channel{N}_Dimm{D}:
  EDAC (mc_event)  "CPU_SrcID#S_MC#M_Chan#C_DIMM#D" (skx: N = M*3+C) or
                   "CPU_SrcID#S_Ha#H_Chan#C_DIMM#D" (sb: N = H*2+C): the exact DIMM
  MCE              socket from socketid, channel from bank + MCACOD (mcelog's
                   *_memerr_misc tables); the DIMM index is not in an MCE, so the key
                   ends at _Dimm and matches every DIMM on that channel
      Skylake/Cascade Lake-SP (model 0x55): banks 7/8 = M2M of iMC 0/1, channel in
                   MCACOD[3:0]; banks 13-18 = one fixed iMC channel each
      Haswell/Broadwell-EP (0x3f/0x4f): banks 7/8 = home agent 0/1, channel in
                   MCACOD[3:0]; banks 9-12 = HA0 ch0-3, 13-16 = HA1 ch0-3
When EDAC decoded any event, only the EDAC events count: the same error also shows as
an MCE and must not count twice."""
import re

_MCE = re.compile(r"status=0x([0-9a-f]+).*?cpuid=0x([0-9a-f]+).*?socketid=0x([0-9a-f]+)"
                  r".*?bank=0x([0-9a-f]+)", re.I)
_EDAC = re.compile(r"CPU_SrcID#(\d+)_(MC|Ha)#(\d+)_Chan#(\d+)_DIMM#(\d+)")
_EDAC_UE = re.compile(r"uncorrect|fatal", re.I)
_UC = 1 << 61
SKX, HSX, BDX = 0x55, 0x3F, 0x4F
_SKX_IMC = {13: (0, 0), 14: (0, 1), 15: (1, 0), 16: (1, 1), 17: (0, 2), 18: (1, 2)}
# dimmerr's trailing field: "DIMM B2   sn: 25783020  ce: 0  ue: 0  at: _Node1_Channel2_Dimm0"
_SLOT = re.compile(r"^(\S[^\n]*?)\s+sn:\s*(\S*)\s+ce:.*?\bat:\s*(_Node\d+_Channel\d+_Dimm\d+)",
                   re.M)


def _model(cpuid: int) -> "int | None":
    if (cpuid >> 8) & 0xF != 6:
        return None
    return ((cpuid >> 4) & 0xF) | ((cpuid >> 12) & 0xF0)


def _channel(model, bank, mcacod) -> "int | None":
    """dimmerr's per-socket channel index N, or None when the bank is not a mapped
    memory bank of a mapped CPU."""
    chan = mcacod & 0xF
    if model == SKX:
        if bank in (7, 8):
            return None if chan == 0xF else (bank - 7) * 3 + chan
        if bank in _SKX_IMC:
            mc, ch = _SKX_IMC[bank]
            return mc * 3 + ch
    elif model in (HSX, BDX):
        if bank in (7, 8):
            return None if chan == 0xF else (bank - 7) * 2 + chan
        if 9 <= bank <= 12:
            return bank - 9
        if 13 <= bank <= 16:
            return 2 + (bank - 13)
    return None


def locate(event: str) -> "dict | None":
    """{key, where, ue, source} for one rasdaemon event line, or None when it is not a
    memory error this can place."""
    m = _EDAC.search(event)
    if m:
        s, kind, unit, c, d = m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), m.group(5)
        n = unit * 3 + c if kind == "MC" else unit * 2 + c
        return {"key": f"_Node{s}_Channel{n}_Dimm{d}", "where": f"socket {s} channel {n} dimm {d}",
                "ue": bool(_EDAC_UE.search(event)), "source": "edac"}
    m = _MCE.search(event)
    if not m:
        return None
    status, cpuid, socket, bank = (int(g, 16) for g in m.groups())
    mcacod = status & 0xFFFF
    if (mcacod & 0xEFFF) >> 7 != 1:          # 0000 0000 1MMM CCCC: memory controller error
        return None
    n = _channel(_model(cpuid), bank, mcacod)
    if n is None:
        return None
    return {"key": f"_Node{socket}_Channel{n}_Dimm", "where": f"socket {socket} channel {n}",
            "ue": bool(status & _UC), "source": "mce"}


def tally(events: list) -> list:
    """[{key, where, ce, ue}] per located slot, in first-seen order."""
    locs = [loc for loc in (locate(e) for e in events or []) if loc]
    if any(loc["source"] == "edac" for loc in locs):
        locs = [loc for loc in locs if loc["source"] == "edac"]
    out: dict = {}
    for loc in locs:
        t = out.setdefault(loc["key"], {"key": loc["key"], "where": loc["where"], "ce": 0, "ue": 0})
        t["ue" if loc["ue"] else "ce"] += 1
    return list(out.values())


def _counts(t) -> str:
    parts = ([f"{t['ce']} correctable"] if t["ce"] else []) + \
            ([f"{t['ue']} uncorrectable"] if t["ue"] else [])
    return ", ".join(parts)


def name(tallies: list, dimmerr_text: "str | None") -> list:
    """One line per tallied slot: the silkscreen + serial from dimmerr's at: field, else
    the socket/channel with "(no dimmerr slot)"."""
    slots = [(m.group(1).strip(), m.group(2), m.group(3))
             for m in _SLOT.finditer(dimmerr_text or "")]
    lines = []
    for t in tallies or []:
        hit = [f"{label} (sn {sn})" for label, sn, key in slots
               if key == t["key"] or (t["key"].endswith("_Dimm") and key.startswith(t["key"]))]
        lines.append(f"{' or '.join(hit) if hit else t['where'] + ' (no dimmerr slot)'}: "
                     f"{_counts(t)}")
    return lines
