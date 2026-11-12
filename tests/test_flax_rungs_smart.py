"""flax_rungs.smart: the Qualify smart-health rung's per-drive table and headline,
from the post.tgz agent's smart-health summary + the inventory's alldisks (slot)."""
from flax_rungs import smart

ALLDISKS = (
    "/dev/sdb     [0:0:0:0] host0  Rear_ArrayDevice03 mpt3sas      rotational      6001.1     ATA SN05 ST6000NM019B-2TG   WX01DSV5\n"
    "/dev/sdc     [0:0:1:0] host0  Rear_ArrayDevice02 mpt3sas      rotational           0     ATA SN05 ST6000NM019B-2TG   WX01DSVB\n"
    "/dev/sda     [2:0:0:0] host2  0:0 ahci         non-rotational   250.0     ATA 10WD Sandisk_SSD_PLUS   2529DXD03109\n"
    "/dev/nvme0n1      [nvme0] 8224       nvme         non-rotational   256.0    none HPS2T    WD_PC_SN810_SDCQNRY-256G-1006 22292V800145\n")


def _dev(dev, sn, verdict="ok", reasons=(), model="ST6000NM019B-2TG103", hours=14200):
    return {"dev": dev, "serial": sn, "model": model, "health": "PASSED",
            "power_on_hours": hours, "verdict": verdict, "reasons": list(reasons)}


def test_location_is_the_enclosure_slot_else_the_onboard_port():
    assert smart.slots(ALLDISKS) == {"sdb": "Rear_ArrayDevice03", "sdc": "Rear_ArrayDevice02",
                                     "sda": "SATA port 0:0", "nvme0n1": "NVMe nvme0"}
    assert smart.slots(None) == {}


def test_summarize_one_failed_drive_names_it_in_the_headline():
    summ = {"devices": [_dev("sda", "2529DXD03109", model="Sandisk SSD PLUS 250GB"),
                        _dev("sdb", "WX01DSV5"),
                        _dev("sdc", "WX01DSVB", "failed", ["SMART self-assessment UNKNOWN!"]),
                        _dev("nvme0n1", "22292V800145", "warn", ["85% of rated write endurance used"])],
            "failed": ["sdc"], "warn": ["nvme0n1"]}
    sm = smart.summarize(summ, ALLDISKS)
    assert sm["headline"] == "4 drives · 1 failed: WX01DSVB (Rear_ArrayDevice02) · 1 warn"
    assert sm["table"]["cols"] == smart.COLS
    rows = {r[0]: r for r in sm["table"]["rows"]}
    assert rows["sdc"][1:6] == ["Rear_ArrayDevice02", "ST6000NM019B-2TG103", "WX01DSVB",
                                "failed", "14200"]
    assert rows["sdc"][6] == "SMART self-assessment UNKNOWN!"
    assert rows["sda"][1:4] == ["SATA port 0:0", "Sandisk SSD PLUS 250GB", "2529DXD03109"]
    assert rows["nvme0n1"][1] == "NVMe nvme0" and rows["nvme0n1"][4] == "warn"
    assert ["failed", "sdc WX01DSVB (Rear_ArrayDevice02): SMART self-assessment UNKNOWN!"] in sm["rows"]
    assert any("left out of fio" in n for n in sm["notes"])


def test_summarize_all_ok_and_nothing_read():
    sm = smart.summarize({"devices": [_dev("sdb", "WX01DSV5")], "failed": [], "warn": []}, ALLDISKS)
    assert sm["headline"] == "1 drive · SMART ok"
    assert smart.summarize({}, None)["headline"] is None
    assert smart.summarize({"reason": "no physical storage media"}, None)["table"]["rows"] == []
