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
