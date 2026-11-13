"""fio archive miner: join, drop noisy samples, clip, group, what-if."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import flax_post
from flax_rack.fakes import _fio_job

SCRIPT = Path(flax_post.__file__).resolve().parent.parent / "scripts" / "fio-baseline-mine"
_l = importlib.machinery.SourceFileLoader("fio_baseline_mine", str(SCRIPT))
mine = importlib.util.module_from_spec(importlib.util.spec_from_loader("fio_baseline_mine", _l))
_l.exec_module(mine)

ALLDISKS = (
    "/dev/sdb     [0:0:0:0] host0  Rear_ArrayDevice03 mpt3sas      rotational      6001.1     "
    "ATA SN05 ST6000NM019B-2TG   WX01DSV5\n"
    "/dev/nvme0n1      [nvme0] 8224       nvme         non-rotational   256.0    none HPS2T    "
    "WD_PC_SN810_SDCQNRY-256G-1006 22292V800145\n")
HWINFO = '''
31: SCSI 100.0: 10600 Disk
  [Created at block.245]
  Hardware Class: disk
  Model: "Sandisk SSD PLUS 250GB"
  Driver: "ahci", "sd"
  Device File: /dev/sda
  Size: 488397168 sectors a 512 bytes
'''


def _out(job, mbps):
    args = {"bw": ("read", "64.0KiB", 64), "lat": ("randread", "4096B", 1),
            "iops": ("read", "4096B", 256)}[job]
    t = _fio_job("x", job, *args, "50.1k", int(mbps), mbps, "29.84", "usec", 5145, "usec")
    return t.split("\n", 1)[1]


def _node(order_dir, post, host, alldisks=None, hwinfo=None):
    p = order_dir / f"post-{post}" / "latest"
    p.mkdir(parents=True)
    (p / "hostname.txt").write_text(host + "\n")
    if alldisks:
        (p / "alldisks-v3.txt").write_text(alldisks)
    if hwinfo:
        (p / "hwinfo.txt").write_text(hwinfo)


def _fio(order_dir, host, dev, job, mbps, mtime=None):
    d = order_dir / "fio-results" / host
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{job}-test-job-{dev}.out"
    f.write_text(_out(job, mbps))
    if mtime:
        os.utime(f, (mtime, mtime))


def _tree(tmp_path):
    n = tmp_path / "bang-x" / "nodes"
    o1, o2 = n / "SESORD-1", n / "SESORD-2"
    _node(o1, "aa", "h1", alldisks=ALLDISKS)
    _fio(o1, "h1", "sdb", "bw", 200)
    _fio(o1, "h1", "nvme0n1", "bw", 3000)
    _fio(o1, "h1", "sdz", "bw", 1)  # absent from inventory
    _node(o2, "bb", "h2", hwinfo=HWINFO)
    _fio(o2, "h2", "sda", "bw", 500)
    _fio(o2, "ghost", "sda", "bw", 500)  # no hostname join
    return tmp_path, n


def test_scan_joins_hostname_and_drops_mismatch(tmp_path):
    root, _ = _tree(tmp_path)
    samples, c = mine.scan(root)
    got = {(s["host"], s["dev"], s["model"], s["controller"]) for s in samples}
    assert got == {("h1", "sdb", "ST6000NM019B-2TG", "mpt3sas"),
                   ("h1", "nvme0n1", "WD PC SN810 SDCQNRY-256G-1006", "nvme"),
                   ("h2", "sda", "SANDISK SSD PLUS", "ahci")}
    assert c["dropped_mismatch"] == 1 and c["unjoined"] == 1


def test_reruns_keep_latest_and_old_dirs_skipped(tmp_path):
    root, n = _tree(tmp_path)
    r2 = n / "SESORD-1r2"
    _node(r2, "aa", "h1", alldisks=ALLDISKS)
    _fio(r2, "h1", "sdb", "bw", 210, mtime=2_000_000_000)
    _fio(n / "SESORD-1" , "h1", "sdb", "bw", 200, mtime=1_000_000_000)
    old = n / "SESORD-1.old"
    _node(old, "aa", "h1", alldisks=ALLDISKS)
    _fio(old, "h1", "sdb", "bw", 1)
    (n / "SESORD-3.pre-renetwork").mkdir()
    samples, c = mine.scan(root)
    sdb = [s for s in samples if s["dev"] == "sdb"]
    assert len(sdb) == 1 and sdb[0]["read"]["mbps"] > 209  # the r2 run
    assert c["dropped_dup"] == 1


def test_unreadable_dir_is_counted_not_fatal(tmp_path):
    root, n = _tree(tmp_path)
    bad = n / "SESORD-9"
    bad.mkdir()
    bad.chmod(0)
    try:
        samples, c = mine.scan(root)
    finally:
        bad.chmod(0o755)
    assert samples
    assert os.geteuid() == 0 or c["unreadable"] >= 1


def test_sigma_clip_removes_outlier():
    r = mine.sigma_clip([100.0] * 20 + [10.0])
    assert r["clipped"] == 1 and r["mean"] == 100 and r["n"] == 20


def test_build_groups_and_any_controller_pool(tmp_path):
    n = tmp_path / "bang-x" / "nodes"
    for i in range(3):
        o = n / f"SESORD-{i}"
        _node(o, "m", f"h{i}", alldisks=ALLDISKS)
        _fio(o, f"h{i}", "sdb", "bw", 200 + i)
    o = n / "SESORD-9"
    _node(o, "m", "hx", alldisks=ALLDISKS.replace("mpt3sas      rotational", "      rotational"))
    _fio(o, "hx", "sdb", "bw", 300)
    samples, c = mine.scan(tmp_path)
    b = mine.build(samples, c)
    assert b["suite"] == "v1" and set(b["jobs"]) == {"bw", "lat", "iops"}
    g = {(x["controller"], x["metric"]): x for x in b["groups"] if x["model"] == "ST6000NM019B-2TG"}
    assert g[("mpt3sas", "mbps")]["n"] == 3
    assert g[(None, "mbps")]["n"] == 4
    assert {"min", "max", "from", "to", "std", "mean", "clipped"} <= set(g[(None, "mbps")])


def test_whatif_reports_flag_counts():
    samples = [{"model": "M", "controller": None, "job": "bw", "date": "2026-01-01",
                "read": {"mbps": v}} for v in [100.0] * 8 + [90.0, 110.0]]
    b = mine.build(samples)
    t = mine.whatif(samples, b)
    row = [ln for ln in t.splitlines() if ln.startswith("M | any | bw")][0]
    assert row.split(" | ")[-1] == "1"  # only the 90 is below mean - 1σ


SMART = ("smartctl 7.4\n=== START OF INFORMATION SECTION ===\nDevice Model:     HGST HUS726060ALE611\n"
         "Serial Number:    SER-A\n\n"
         "smartctl 7.4\n=== START OF INFORMATION SECTION ===\nModel Number:     SAMSUNG MZQL21T9HCJR-00A07\n"
         "Serial Number:    SER-N\n")
HW2 = '''
31: SCSI 100.0: 10600 Disk
  Hardware Class: disk
  Model: "WESTERN DIGITAL DISK"
  Serial ID: "SER-A"
  Driver: "ahci", "sd"
  Device File: /dev/sda
2: PCI 1: 10600 Disk
  Hardware Class: disk
  Model: "SAMSUNG ELECTRONICS NVME SSD CONTROLLER SM981/PM981/PM983"
  Serial ID: "SER-N"
  Driver: "nvme"
  Device File: /dev/nvme0n1
3: SCSI 101.0: 10600 Disk
  Hardware Class: disk
  Model: "MICRON DISK"
  Serial ID: "NOPE"
  Device File: /dev/sdc
'''


def test_smartctl_serial_beats_generic_hwinfo_and_unknown_is_unjoined(tmp_path):
    p = tmp_path / "latest"
    p.mkdir()
    (p / "hwinfo.txt").write_text(HW2)
    (p / "smartctl--all.txt").write_text(SMART)
    d = mine.node_disks(p)
    assert d["sda"]["model"] == "HGST HUS726060ALE611" and d["sda"]["controller"] == "ahci"
    assert d["nvme0n1"]["model"] == "SAMSUNG MZQL21T9HCJR-00A07"
    assert d["sdc"]["model"] is None  # generic hwinfo name, no smartctl match


def test_hwinfo_generic_without_smartctl_not_grouped(tmp_path):
    p = tmp_path / "latest"
    p.mkdir()
    (p / "hwinfo.txt").write_text(HW2)
    d = mine.node_disks(p)
    assert all(v["model"] is None for v in d.values())


def test_smartctl_block_naming_its_device(tmp_path):
    p = tmp_path / "latest"
    p.mkdir()
    (p / "smartctl--all.txt").write_text(
        "smartctl 7.4 ... \nSmartctl open device: /dev/sdb\n=== START ===\nProduct:  HUS726060AL5210\n")
    assert mine.node_disks(p)["sdb"]["model"] == "HUS726060AL5210"


def test_miner_key_equals_runtime_judge_key_for_same_drive():
    from flax_post import fio_judge as fj
    from flax_rungs.fio import disk_info
    rt = disk_info(ALLDISKS)["sdb"]["model"]            # runtime alldisks form
    smart = "ST6000NM019B-2TG103"                        # smartctl form
    assert fj.normalise_model(rt, "sdb") == fj.normalise_model(smart, "sdb")


def _smp(host, dev, mbps, model="M1"):
    return {"model": model, "controller": None, "job": "bw", "dev": dev, "host": host,
            "order": "O", "date": "2026-01-01", "read": {"mbps": mbps}}


def test_whatif_sibling_section_counts_flags_per_model():
    samples = [_smp("h", "sda", 200), _smp("h", "sdb", 200), _smp("h", "sdc", 100),  # sdc 50% worse
               _smp("h2", "sda", 200, "M2"), _smp("h2", "sdb", 100, "M2")]            # < 3: skipped
    base = mine.build(samples)
    txt = mine.whatif(samples, base)
    assert "M1 | 3 | 1" in txt and "M2" not in txt.split("sibling check")[1]
    assert "TOTAL | 3 | 1" in txt


def test_distinct_models_stay_distinct_for_non_nvme():
    from flax_post import fio_judge as fj
    assert fj.normalise_model("Sandisk_SSD_PLUS", "sda") == fj.normalise_model("SanDisk SSD PLUS 1000GB", "sda")
    assert fj.normalise_model("SanDisk SSD PLUS 1000GB", "sda") != fj.normalise_model("SanDisk SSD ULTRA 1000GB", "sda")
    assert fj.normalise_model("Samsung SSD 860 EVO 1TB", "sda") != fj.normalise_model("Samsung SSD 870 EVO 1TB", "sda")


def test_whatif_uses_the_judges_sigma_floor():
    samples = [{"model": "M", "controller": None, "job": "bw", "date": "2026-01-01",
                "read": {"mbps": v}} for v in [500.0] * 9 + [499.0]]
    t = mine.whatif(samples, mine.build(samples))
    row = [ln for ln in t.splitlines() if ln.startswith("M | any | bw")][0]
    assert row.split(" | ")[-1] == "0"  # 499 is a hair under, inside 2 x 1% of the mean


BYID = (
    "/dev/disk/by-id:\ntotal 0\n"
    "lrwxrwxrwx 1 root root 13 Jan 11 22:30 nvme-SAMSUNG_MZ1LB960HAJQ-00007_S435NC0R112452 -> ../../nvme0n1\n"
    "lrwxrwxrwx 1 root root 13 Jan 11 22:30 nvme-eui.0025385a01b0c0d1 -> ../../nvme0n1\n"
    "lrwxrwxrwx 1 root root 15 Jan 11 22:30 nvme-SAMSUNG_MZ1LB960HAJQ-00007_S435NC0R112452-part1 -> ../../nvme0n1p1\n"
    "lrwxrwxrwx 1 root root 13 Jan 11 22:30 nvme-SAMSUNG_MZ1LB960HAJQ-00007_S435NC0R112453 -> ../../nvme1n1\n"
    "lrwxrwxrwx 1 root root  9 Jan 11 22:30 ata-KINGSTON_SKC600MS256G_50026B77842BD7C3 -> ../../sda\n"
    "lrwxrwxrwx 1 root root  9 Jan 11 22:30 wwn-0x5002538 -> ../../sda\n"
    "\n/dev/disk/by-path:\ntotal 0\n"
    "lrwxrwxrwx 1 root root 9 Jan 11 22:30 pci-0000:00:1f.2-ata-1 -> ../../sdz\n")
SMART2 = (
    "smartctl 7.4\n=== START OF INFORMATION SECTION ===\nModel Number:     SAMSUNG MZ1LB960HAJQ-00007\n"
    "Serial Number:    S435NC0R112453\n"
    "smartctl 7.4\n=== START OF INFORMATION SECTION ===\nModel Number:     SAMSUNG MZ1LB960HAJQ-00007\n"
    "Serial Number:    S435NC0R112452\n")
PCI_ONLY = '''
12: PCI 1d00.0: 10802 Non-Volatile memory controller
  Hardware Class: storage
  Model: "Samsung NVMe SSD Controller"
  Driver: "nvme"
'''


def test_byid_serial_maps_nameless_smartctl_blocks(tmp_path):
    n = tmp_path / "bang-x" / "nodes" / "SESORD-7"
    _node(n, "aa", "h7", hwinfo=PCI_ONLY)
    p = n / "post-aa" / "latest"
    (p / "smartctl--all.txt").write_text(SMART2)
    (p / "ls-lR_dev_disk.txt").write_text(BYID)
    for dev in ("nvme0n1", "nvme1n1"):
        _fio(n, "h7", dev, "bw", 1000)
    samples, c = mine.scan(tmp_path)
    assert sorted(s["dev"] for s in samples) == ["nvme0n1", "nvme1n1"]
    for s in samples:
        assert "SAMSUNG" in s["model"].upper()
        assert s["model"] == mine.normalise_model("SAMSUNG MZ1LB960HAJQ-00007", "nvme0n1")
    assert c["unjoined"] == 0
    d = mine.node_disks(p)
    assert "sdz" not in d and d["sda"]["model"] == "KINGSTON SKC600MS256G"


def test_missing_dirs_are_not_unreadable(tmp_path):
    (tmp_path / "bang-empty").mkdir()           # no nodes dir
    n = tmp_path / "bang-x" / "nodes" / "SESORD-1"
    n.mkdir(parents=True)                       # no fio-results
    samples, c = mine.scan(tmp_path)
    assert samples == []
    assert c["missing_dir"] >= 2 and c["unreadable"] == 0


# Real archive strings (bang-fiesta /export/bangs, 2026-11-04).  The two HUS726060 keys are
# two SKUs, not one drive read two ways: E614 (fw APDEKP06, bang-lulea) reports
# "HGST HUS726060ALE614" in IDENTIFY, E611 (fw APGL0001, bang-kc) reports "HUS726060ALE611";
# no serial appears under both.  Every source for one SKU must land on one key.
HUH_ALLDISKS = ("/dev/sdb     [6:0:0:0] host6  0:0 ahci         rotational      8001.5     "
                "ATA T907 HGST_HUH728080AL   2EJE6K3X\n")   # runtime alldisks: vendor ATA, HGST in model
E614_BYID = ("/dev/disk/by-id:\ntotal 0\n"
             "lrwxrwxrwx 1 root root  9 Aug  3  2020 ata-HGST_HUS726060ALE614_K1HMWM7D -> ../../sda\n"
             "lrwxrwxrwx 1 root root  9 Aug  3  2020 scsi-SATA_HGST_HUS726060AL_K1HMWM7D -> ../../sda\n")
E614_SMART = ("smartctl 6.6 2017-11-05 r4594\n=== START OF INFORMATION SECTION ===\n"
              "Device Model:     HGST HUS726060ALE614\nSerial Number:    K1HMWM7D\n"
              "Firmware Version: APDEKP06\n")
E614_HW = ('\n31: SCSI 100.0: 10600 Disk\n  Hardware Class: disk\n  Model: "HGST HUS726060AL"\n'
           '  Serial ID: "K1HMWM7D"\n  Driver: "ahci", "sd"\n  Device File: /dev/sda\n')
E611_BYID = ("/dev/disk/by-id:\ntotal 0\n"
             "lrwxrwxrwx 1 root root  9 Oct 12 14:44 ata-HUS726060ALE611_NAGAVRAX -> ../../sda\n")
E611_SMART = ("smartctl 6.6 2017-11-05 r4594\n=== START OF INFORMATION SECTION ===\n"
              "Device Model:     HUS726060ALE611\nSerial Number:    NAGAVRAX\n")
E611_HW = ('\n31: SCSI 100.0: 10600 Disk\n  Hardware Class: disk\n  Model: "HUS726060ALE611"\n'
           '  Serial ID: "NAGAVRAX"\n  Driver: "ahci", "sd"\n  Device File: /dev/sda\n')


def _latest(tmp_path, name, smart=None, byid=None, hw=None):
    p = tmp_path / name
    p.mkdir()
    for f, t in (("smartctl--all.txt", smart), ("ls-lR_dev_disk.txt", byid), ("hwinfo.txt", hw)):
        if t:
            (p / f).write_text(t)
    return mine.node_disks(p)["sda"]["model"]


def test_runtime_alldisks_keeps_the_vendor_word_hgst_in_its_model_field():
    from flax_rungs.fio import disk_info
    rt = disk_info(HUH_ALLDISKS)["sdb"]["model"]
    assert rt == "HGST_HUH728080AL"
    assert mine.normalise_model(rt, "sdb") == mine.normalise_model("HGST HUH728080ALN600", "sdb")


def test_every_source_for_one_hus726060_sku_gives_one_key(tmp_path):
    runtime_e614 = mine.normalise_model("HGST_HUS726060AL", "sda")   # sysfs model, as alldisks prints it
    for i, kw in enumerate([dict(smart=E614_SMART, byid=E614_BYID, hw=E614_HW),
                            dict(byid=E614_BYID, hw=E614_HW), dict(hw=E614_HW)]):
        assert mine.normalise_model(_latest(tmp_path, f"a{i}", **kw), "sda") == runtime_e614
    runtime_e611 = mine.normalise_model("HUS726060ALE611", "sda")
    for i, kw in enumerate([dict(smart=E611_SMART, byid=E611_BYID, hw=E611_HW),
                            dict(byid=E611_BYID, hw=E611_HW), dict(hw=E611_HW)]):
        assert mine.normalise_model(_latest(tmp_path, f"b{i}", **kw), "sda") == runtime_e611
    assert runtime_e614 != runtime_e611   # two part numbers stay two keys


def test_smartctl_model_beats_byid_model_for_the_same_serial(tmp_path):
    byid = E614_BYID.replace("HGST_HUS726060ALE614", "SOMEOTHER_NAME")
    assert _latest(tmp_path, "c", smart=E614_SMART, byid=byid) == "HGST HUS726060ALE614"


def test_whatif_lists_groups_set_aside_by_the_spread_cap():
    from flax_post import fio_judge as fj
    samples = [{"model": "WIDE", "controller": None, "job": "lat", "date": "2026-01-01",
                "read": {"lat_us": 100.0, "p99_us": v}} for v in [100.0, 200.0] * 5]
    b = mine.build(samples)
    t = mine.whatif(samples, b).split("set aside by the spread cap")[1].split("sibling check")[0]
    rows = [ln for ln in t.splitlines() if ln.startswith("WIDE")]
    assert t.splitlines()[0].rstrip().endswith(": 1") and rows == [
        "WIDE | any | lat | p99_us | 10 | 150 | 50 | 33.3"]
    # the group stays in the baseline untouched
    g = [x for x in b["groups"] if x["metric"] == "p99_us"][0]
    assert (g["n"], g["mean"], g["std"]) == (10, 150.0, 50.0) and fj.too_wide(g)
