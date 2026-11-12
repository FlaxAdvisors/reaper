"""flax_rungs.mcedimm: rasdaemon memory events -> the DIMM slot. Built from LVQ et7b4's
deploy-stress 2026-11-10 (2 corrected MCEs on socket 1, bank 7, channel 2 that dimmerr
never saw: the kernel's correctable-error collector kept them from skx_edac)."""
from flax_post import deploy
from flax_rungs import mcedimm, stress

ET7B4 = [
    "error: MEMORY CONTROLLER RD_CHANNEL2_ERR Transaction: Memory read error MscodDataRdErr, "
    "mcg mcgstatus=0, mci Corrected_error Error_enabled, n_errors=1, mcgcap=0x0f000c14, "
    "status=0x9c00004001010092, addr=0x509850c2c0, misc=0x200800c017e01086, tsc=0x14308df0904, "
    "walltime=0x6af29726, cpu=0x00000014, cpuid=0x00050654, apicid=0x00000040, "
    "socketid=0x00000001, bank=0x00000007",
    "error: MEMORY CONTROLLER RD_CHANNEL2_ERR Transaction: Memory read error MscodDataRdErr, "
    "mcg mcgstatus=0, mci Corrected_error Error_enabled, n_errors=1, mcgcap=0x0f000c14, "
    "status=0x9c00004001010092, addr=0x50985074c0, misc=0x200801c093e01086, tsc=0x14a837b310c, "
    "walltime=0x6af29736, cpu=0x00000014, cpuid=0x00050654, apicid=0x00000040, "
    "socketid=0x00000001, bank=0x00000007"]

DIMMERR = "".join(
    f"DIMM {s}{i}   sn: {s}{i}SN          ce: 0    ue: 0    at: _Node{n}_Channel{i}_Dimm0\n"
    for n, s in ((0, "A"), (1, "B")) for i in range(6))


def mce(status, bank, socket=0, cpuid="0x00050654"):
    return (f"error: MEMORY CONTROLLER x, status=0x{status:016x}, addr=0x1, misc=0x0, "
            f"cpuid={cpuid}, apicid=0x0, socketid=0x{socket:08x}, bank=0x{bank:08x}")


def test_et7b4_names_dimm_b2():
    t = mcedimm.tally(ET7B4)
    assert t == [{"key": "_Node1_Channel2_Dimm", "where": "socket 1 channel 2", "ce": 2, "ue": 0}]
    assert mcedimm.name(t, DIMMERR) == ["DIMM B2 (sn B2SN): 2 correctable"]


def test_skylake_banks():
    ce = 0x9c00004000010090          # read error, channel nibble 0
    # M2M banks carry the channel in MCACOD; iMC banks 13-18 are one fixed channel each
    assert mcedimm.locate(mce(ce | 1, 8))["key"] == "_Node0_Channel4_Dimm"      # MC1 ch1
    assert mcedimm.locate(mce(ce, 17, socket=1))["key"] == "_Node1_Channel2_Dimm"  # MC0 ch2
    assert mcedimm.locate(mce(ce, 15))["key"] == "_Node0_Channel3_Dimm"          # MC1 ch0
    assert mcedimm.locate(mce(ce | 0xf, 7)) is None                         # channel unknown
    assert mcedimm.locate(mce(0x9c00004000010005, 4)) is None               # not a memory error
    assert mcedimm.locate(mce(ce, 7, cpuid="0x000a06a4")) is None           # model not mapped


def test_broadwell_banks():
    ce, bdx = 0x9c00004000010091, "0x000406f1"     # Leopard: 2 home agents x 2 channels
    assert mcedimm.locate(mce(ce, 8, cpuid=bdx))["key"] == "_Node0_Channel3_Dimm"   # HA1 ch1
    assert mcedimm.locate(mce(ce, 10, cpuid=bdx))["key"] == "_Node0_Channel1_Dimm"  # HA0 ch1
    assert mcedimm.locate(mce(ce, 13, socket=1, cpuid=bdx))["key"] == "_Node1_Channel2_Dimm"


def test_uncorrected_counts_as_ue():
    t = mcedimm.tally([mce(0xbe00000000800090, 7)])                         # UC bit 61 set
    assert t[0]["ue"] == 1 and t[0]["ce"] == 0
    assert mcedimm.name(t, DIMMERR) == ["DIMM A0 (sn A0SN): 1 uncorrectable"]


def test_edac_events_win_and_name_the_exact_dimm():
    # with the collector off, skx_edac decodes too: its event names the DIMM and the
    # matching MCE must not be counted a second time
    edac = ("1 Corrected error(s): memory read error at CPU_SrcID#1_MC#0_Chan#2_DIMM#0 "
            "location: 1:2:-1:-1, addr 346288259776, grain 5, syndrome 0")
    t = mcedimm.tally([edac] + ET7B4[:1])
    assert t == [{"key": "_Node1_Channel2_Dimm0", "where": "socket 1 channel 2 dimm 0",
                  "ce": 1, "ue": 0}]
    assert mcedimm.name(t, DIMMERR) == ["DIMM B2 (sn B2SN): 1 correctable"]
    ha = "memory read error at CPU_SrcID#0_Ha#1_Chan#0_DIMM#1 location"
    assert mcedimm.tally([ha])[0]["key"] == "_Node0_Channel2_Dimm1"


def test_unmapped_slot_still_says_where():
    t = mcedimm.tally(ET7B4)
    assert mcedimm.name(t, None) == ["socket 1 channel 2 (no dimmerr slot): 2 correctable"]
    old = "DIMM B2   sn: B2SN          ce: 0    ue: 0\n"          # dimmerr without at:
    assert mcedimm.name(t, old) == ["socket 1 channel 2 (no dimmerr slot): 2 correctable"]


def test_at_field_leaves_dimmerr_parsers_alone():
    assert deploy.dimm_counts(DIMMERR)["DIMM B2"] == (0, 0)
    assert len(deploy.dimm_counts(DIMMERR)) == 12


def test_stress_hw_events_carries_the_slots():
    import datetime
    ras = "MCE events:\n" + "".join(
        f"{i} 2026-11-10 04:01:{40 + i} +0000 {e}\n" for i, e in enumerate(ET7B4, 1))
    t0 = datetime.datetime(2026, 11, 10, 3, 55, tzinfo=datetime.timezone.utc).timestamp()
    ev = stress.hw_events([], ras, t0)
    assert ev["corrected"] == 2
    assert ev["mem_errors"] == mcedimm.tally(ET7B4)
