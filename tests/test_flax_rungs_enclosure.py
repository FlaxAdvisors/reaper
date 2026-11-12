"""Enclosures judged through their head's PCIe (plan 2026-11-02 Task 7). Fixtures: the
howlin heads' real `lspci -vv` / `lsscsi -c` (2026-11-02): .53 Big Basin head (4 V100),
.56 Bryce Canyon head (HBA 9400-8e, JBOD dark), .69 nothing attached."""
import pathlib
import pytest
pytest.importorskip("flax_rungs")
from flax_rungs.enclosure import facts, judge

F = pathlib.Path(__file__).parent / "fixtures/rackpost/enclosure"


def _f(h):
    return facts((F / f"howlin-{h}-lspci-vv.txt").read_text(), (F / f"howlin-{h}-lsscsi-c.txt").read_text())


# a Lightning head as the 2026-10-28 capture shows it: PM8536 upstream 10:00.0 (bus
# 11-22), one Samsung PM9A3 at 1f:00.0 behind it, the boot NVMe elsewhere (01:00.0)
LIGHTNING = """00:02.0 PCI bridge: Intel Corporation Xeon E5 v4 PCI Express Root Port 2 (rev 01)
\tBus: primary=00, secondary=10, subordinate=22, sec-latency=0
01:00.0 Non-Volatile memory controller: Sandisk Corp WD Black SN750 (prog-if 02 [NVM Express])
\t\tLnkSta:\tSpeed 8GT/s, Width x4
10:00.0 PCI bridge: PMC-Sierra Inc. PM8536 PFX 96xG3 PCIe Fanout Switch (rev 05)
\tBus: primary=10, secondary=11, subordinate=22, sec-latency=0
11:0d.0 PCI bridge: PMC-Sierra Inc. PM8536 PFX 96xG3 PCIe Fanout Switch (rev 05)
\tBus: primary=11, secondary=1f, subordinate=1f, sec-latency=0
1f:00.0 Non-Volatile memory controller: Samsung Electronics Co Ltd NVMe SSD Controller PM9A1/PM9A3/980PRO (prog-if 02 [NVM Express])
\t\tLnkSta:\tSpeed 8GT/s, Width x4
"""


def test_facts_big_basin_head():
    f = _f(53)
    assert (f["gpu"], f["gpu_x16"]) == (4, 4) and f["plx"] >= 9 and not f["pm8536"]


def test_bb_head_judged_against_bom():
    r = judge("bb-head", {"gpu": 8}, _f(53))
    assert r.verdict == "fail" and r.detail["headline"] == "4/8 V100 (x16 4/4)"
    f = dict(_f(53), gpu=8, gpu_x16=8)
    assert judge("bb-head", {"gpu": 8}, f).verdict == "pass"


def test_bc_head_without_jbod_fails():
    f = _f(56)
    assert f["sas_hba"] and not f["sas_expander"] and f["disks"] == 0
    r = judge("bc-head", {"disks": 4}, f)
    assert r.verdict == "fail" and r.detail["headline"] == "no SAS expander"


def test_lightning_counts_only_nvme_behind_the_switch():
    f = facts(LIGHTNING, "")
    assert f["pm8536"] and f["nvme_behind_switch"] == 1
    r = judge("lightning-head", {"nvme": 4}, f)
    assert r.verdict == "fail" and r.detail["headline"] == "1/4 NVMe behind the PM8536"


def test_nothing_attached_names_it():
    r = judge("bb-head", {"gpu": 8}, _f(69))
    assert r.verdict == "fail" and r.detail["headline"] == "0/8 V100 (no Big Basin link)"
