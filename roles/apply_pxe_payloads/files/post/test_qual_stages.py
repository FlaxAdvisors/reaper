import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import qual_stages as qs


def test_sel_digest_counts_unique_events_dropping_timestamp():
    sel = ("1 | 01/01/2026 | 12:00:00 | Processor CATERR | Asserted\n"
           "2 | 01/01/2026 | 12:00:05 | Fan #1 | Lower Critical\n"
           "3 | 01/02/2026 | 03:00:00 | Fan #1 | Lower Critical\n")
    digest, summary = qs.sel_digest(sel)
    # the two Fan events at DIFFERENT times must collapse to one line, count 2
    assert "2 Fan #1 | Lower Critical" in digest
    assert summary["unique_msgs"] == 2       # CATERR + Fan (two distinct events)
    assert summary["caterr"] is True


def test_sel_recent_keeps_last_n_in_time_order():
    lines = "".join(
        "%d | 01/01/2026 | 12:00:%02d | Fan #1 | Lower Critical\n" % (i, i)
        for i in range(60))
    out = qs.sel_recent(lines, n=50)
    kept = out.splitlines()
    assert len(kept) == 50
    assert kept[0].startswith("10 |")    # oldest 10 dropped
    assert kept[-1].startswith("59 |")   # most-recent kept


def test_runner_success_and_missing_binary():
    rc, out = qs.RUNNER(["/bin/echo", "hi"], 5)
    assert rc == 0 and "hi" in out
    rc, out = qs.RUNNER(["/nonexistent-binary-xyz-123"], 5)
    assert rc == 127 and "not found" in out


def test_runner_survives_exec_format_error(tmp_path):
    # a shebang-less executable text file (like the real /opt/flax/bin/dimmsum) makes
    # execve fail ENOEXEC -> OSError; RUNNER must degrade to rc 126, not raise (else it
    # crashes the whole inventory stage, as seen live on fl001-et10b4 2026-07-15).
    import os
    f = tmp_path / "noshebang"
    f.write_text("# just text, no shebang\nsome data\n")
    os.chmod(str(f), 0o755)
    rc, out = qs.RUNNER([str(f)], 5)
    assert rc == 126 and "noshebang" in out


def test_stress_cmd_computes_threads_and_memory():
    lscpu = "Socket(s):             2\nCore(s) per socket:    8\nThread(s) per core:    2\n"
    meminfo = "MemTotal:       65536000 kB\nMemAvailable:   60000000 kB\n"
    cmd = qs.stress_cmd(lscpu, meminfo, duration=60)
    # total=2*8*2=32 -> cpu=16, mem=15; command well-formed
    assert cmd.startswith("stress --cpu 16 -m 15 --vm-bytes ")
    assert cmd.endswith("--timeout 60")


def test_edac_totals_sums_ce_ue():
    txt = ("DIMM_A0   sn: 111   ce: 3   ue: 0\n"
           "DIMM_B0   sn: 222   ce: 1   ue: 2\n")
    out = qs.edac_totals(txt)
    assert out == {"ce": 4, "ue": 2, "dimms": 2}


# --- smart_health: one device's `smartctl --all` -> ok | warn | failed ---------------
_SMART = os.path.join(os.path.dirname(__file__), "testdata", "smart")


def _smart(serial):
    with open(os.path.join(_SMART, serial + ".txt")) as f:
        return qs.smart_health(f.read())


def test_smart_health_real_ata_and_nvme_drives_pass():
    for sn in ("2529DXD03109", "WX01DSV5", "WX01DSV8", "1926FR466409", "WX01DSV6",
               "22292V800145"):
        h = _smart(sn)
        assert (h["serial"], h["verdict"], h["reasons"]) == (sn, "ok", []), sn
        assert h["health"] == "PASSED" and h["model"]


def test_smart_health_unknown_self_assessment_fails():
    # RRORD-101 L05 Rear_ArrayDevice02 (2026-11-03): every SMART read aborted
    h = _smart("WX01DSVB")
    assert h["verdict"] == "failed"
    assert "SMART self-assessment UNKNOWN!" in h["reasons"]


_ATA = ("smartctl 7.4\n=== START OF INFORMATION SECTION ===\nDevice Model:     X\n"
        "Serial Number:    S1\n=== START OF READ SMART DATA SECTION ===\n"
        "SMART overall-health self-assessment test result: PASSED\n"
        "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE\n"
        "  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       %d\n"
        "  9 Power_On_Hours          0x0032   084   084   000    Old_age   Always       -       14200 (12 34 0)\n"
        "197 Current_Pending_Sector  0x0012   100   100   000    Old_age   Always       -       0\n")


def test_smart_health_reallocated_sectors_fail_and_hours_are_read():
    assert _smart_text(_ATA % 0)["verdict"] == "ok"
    h = _smart_text(_ATA % 8)
    assert h["verdict"] == "failed" and h["reasons"] == ["reallocated sectors: 8"]
    assert h["power_on_hours"] == 14200


def _smart_text(t):
    return qs.smart_health(t)


_NVME = ("=== START OF INFORMATION SECTION ===\nModel Number:   N\nSerial Number:  S2\n"
         "=== START OF SMART DATA SECTION ===\n"
         "SMART overall-health self-assessment test result: PASSED\n"
         "Critical Warning:                   0x00\nAvailable Spare:                    %d%%\n"
         "Available Spare Threshold:          10%%\nPercentage Used:                    %d%%\n"
         "Media and Data Integrity Errors:    %d\nPower On Hours:                     1,234\n")


def test_smart_health_nvme_wear_warns_and_media_errors_fail():
    assert _smart_text(_NVME % (100, 3, 0))["verdict"] == "ok"
    w = _smart_text(_NVME % (100, 85, 0))
    assert w["verdict"] == "warn" and w["reasons"] == ["85% of rated write endurance used"]
    assert _smart_text(_NVME % (100, 3, 2))["verdict"] == "failed"
    assert _smart_text(_NVME % (10, 3, 0))["verdict"] == "failed"       # spare at threshold
    assert _smart_text(_NVME % (100, 3, 0))["power_on_hours"] == 1234


def test_smart_health_sas_status_and_grown_defects():
    sas = ("=== START OF INFORMATION SECTION ===\nProduct:  P\nSerial number:  S3\n"
           "SMART Health Status: OK\nElements in grown defect list: %d\n")
    assert _smart_text(sas % 0)["verdict"] == "ok"
    h = _smart_text(sas % 4)
    assert h["verdict"] == "failed" and h["reasons"] == ["grown defect list: 4"]
    assert _smart_text(sas % 0)["serial"] == "S3"


def test_smart_health_never_passes_on_nothing():
    # no report at all, or a report with no self-assessment line: failed, never ok
    for t in ("", "/dev/sdz: Unable to detect device type\n",
              "=== START OF INFORMATION SECTION ===\nSerial Number: S4\nSMART support is: Unavailable\n"):
        h = _smart_text(t)
        assert h["verdict"] == "failed" and h["reasons"], t
