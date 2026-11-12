"""flax_rungs.fio: the fio stage's per-device summary from the agent's artifact (real
format: qual_battery._fio's `== <dev> <job> ==` + fio's default output; values made up)."""
import pytest

pytest.importorskip("flax_rungs.fio")
from flax_rack.fakes import _ALLDISKS, _FIO, _fio_job
from flax_rungs import fio

# an sda whose latencies print in msec / nsec (fio picks the unit per figure)
SDA = "\n".join(_fio_job("sda", *j) for j in (
    ("bw", "read", "64.0KiB", 64, "8627", 539, 565, "29.66", "msec", 30, "msec"),
    ("lat", "randread", "4096B", 1, "66.2k", 259, 271, "57830", "nsec", 58368, "nsec")))
ALLDISKS = _ALLDISKS + ("/dev/sda     [2:0:0:0] host2  0:0 ahci         non-rotational   250.0     "
                        "ATA 10WD Sandisk_SSD_PLUS   2529DXD03109\n")


def test_table_rows_per_device_and_job_and_headline():
    s = fio.summarize(_FIO, ["nvme0n1", "nvme1n1"], _ALLDISKS)
    assert s["table"]["cols"] == fio.COLS
    rows = s["table"]["rows"]
    assert len(rows) == 6
    assert rows[0] == ["nvme0n1", "SAMSUNG_MZQL21T9HCJR-00A07 · 1920.4 GB",
                       "bw · read 64.0KiB qd64", "50.1k", "3283", "5100", "5145"]
    # lat: avg 29.84 us; the percentiles print in nsec (46400 ns = 46.4 us)
    # job-major: bw for both devices, then lat, then iops
    assert [(r[0], r[2].split(" ")[0]) for r in rows] == [
        (d, j) for j in ("bw", "lat", "iops") for d in ("nvme0n1", "nvme1n1")]
    assert rows[2][2:] == ["lat · randread 4096B qd1", "131k", "537", "29.8", "46.4"]
    assert rows[4][2:5] == ["iops · read 4096B qd256", "812k", "3326"]
    assert s["fio_headline"] == "nvme0n1 812k IOPS · 3283 MB/s · 29.8 µs · +1 device"


def test_units_and_missing_parts_are_named():
    text = SDA + "\n== sdb bw ==\nfio: failed to open /dev/sdb: Permission denied\nerr= 13\n"
    s = fio.summarize(text, ["sda", "sdb", "sdc"], ALLDISKS)
    rows = {(r[0], r[2].split()[0]): r for r in s["table"]["rows"]}
    assert rows[("sda", "bw")][5:] == ["29660", "30000"]           # msec -> µs
    assert rows[("sda", "lat")][5:] == ["57.8", "58.4"]            # nsec -> µs
    assert rows[("sda", "iops")][3] == "job not in the output"
    assert rows[("sdb", "bw")][3] == "not parsed: fio err=13"
    assert rows[("sdc", "—")][3] == "no fio output for this device"
    assert rows[("sda", "bw")][1] == "Sandisk_SSD_PLUS · 250 GB"
    assert s["rows"][1][0] == "missing" and "sdc: no output" in s["rows"][1][1]
    # no nvme: the first device leads; the IOPS job is missing there
    assert s["fio_headline"] == "sda — IOPS · 565 MB/s · 57.8 µs · +2 devices"


def test_size_from_lsblk_without_alldisks():
    lsblk = "NAME MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS\nnvme0n1 259:0 0 238.5G 0 disk \n"
    s = fio.summarize(_FIO, None, None, lsblk)
    assert s["table"]["rows"][0][1] == "238.5G" and s["table"]["rows"][3][1] == "—"


def test_summarize_lists_the_disks_smart_kept_out_of_fio():
    sm = fio.summarize("", ["sda"], None, None,
                       excluded={"sdc": ["SMART self-assessment UNKNOWN!"]})
    assert ["left out (SMART failed)", "sdc: SMART self-assessment UNKNOWN!"] in sm["rows"]
    assert all(r[0] != "sdc" for r in sm["table"]["rows"])


def test_rows_sorted_by_job_then_device():
    devs = ["nvme1n1", "sdb", "nvme0n1", "sda", "sdaa"]
    text = "\n".join(_fio_job(d, *j) for d in devs for j in (
        ("bw", "read", "64.0KiB", 64, "8627", 539, 565, "29.66", "msec", 30, "msec"),
        ("lat", "randread", "4096B", 1, "66.2k", 259, 271, "57830", "nsec", 58368, "nsec"),
        ("iops", "read", "4096B", 256, "66.2k", 259, 271, "57830", "nsec", 58368, "nsec")))
    rows = fio.summarize(text, devs)["table"]["rows"]
    order = ["sda", "sdb", "sdaa", "nvme0n1", "nvme1n1"]
    assert [(r[0], r[2].split(" ")[0]) for r in rows] == [
        (d, j) for j in ("bw", "lat", "iops") for d in order]


def test_natural_dev_key_orders_nvme10_after_nvme9():
    devs = ["nvme10n1", "nvme9n1", "sdaa", "vda", "sdz", "sda", "nvme0n1"]
    assert sorted(devs, key=fio.natural_dev_key) == [
        "sda", "sdz", "sdaa", "nvme0n1", "nvme9n1", "nvme10n1", "vda"]


def test_disk_info_reads_model_controller_serial_size():
    info = fio.disk_info(ALLDISKS)
    assert info["sda"] == {"model": "Sandisk_SSD_PLUS", "controller": "ahci",
                           "serial": "2529DXD03109", "size_gb": 250.0}
    assert info["nvme0n1"]["model"] == "SAMSUNG_MZQL21T9HCJR-00A07"
    assert info["nvme0n1"]["serial"] == "S64GNE0R700123"
    assert fio.disk_info(None) == {}


def test_check_cell_names_a_metric_set_aside_by_the_spread_cap():
    judged = {"nvme0n1": {"verdict": "ok", "reasons": [], "expected": {"lat": "≤ 40 µs"},
                          "spread": {"lat": ["p99_us"]}},
              "nvme1n1": {"verdict": "no baseline", "reasons": [],
                          "expected": {"bw": "no baseline (spread too wide)"},
                          "spread": {"bw": ["mbps"]}}}
    rows = fio.summarize(_FIO, ["nvme0n1", "nvme1n1"], _ALLDISKS, judged=judged)["table"]["rows"]
    chk = {(r[0], r[2].split()[0]): r[-1] for r in rows}
    assert chk[("nvme0n1", "lat")] == "ok · p99 spread too wide"
    assert chk[("nvme0n1", "bw")] == "ok"
    assert chk[("nvme1n1", "bw")] == "no baseline (spread too wide)"
    assert chk[("nvme1n1", "lat")] == "no baseline"
