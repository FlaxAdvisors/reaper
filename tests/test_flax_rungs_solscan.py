from flax_rungs import solscan

CLEAN = ("[  OK  ] Started systemd-journald.service\r\n"
         "\x1b[0;32m  OK  \x1b[0m Reached target Network.\r\n"
         "subiquity/Install: installing system\r\n"
         "curtin command install\r\n"
         "reboot: Power down\r\n")


def test_clean_capture_passes(tmp_path):
    p = tmp_path / "sol.txt"
    p.write_bytes(CLEAN.encode())
    r = solscan.scan_file(p)
    assert r.verdict == "pass", r.detail
    assert r.detail["findings"] == 0 and r.detail["rows"] == []


def test_each_pattern_is_a_finding_row(tmp_path):
    lines = ["[ 12.1] Kernel panic - not syncing: VFS",
             "[ 13.0] Call Trace:",
             "subiquity/Error: Traceback (most recent call last)",
             "subiquity.server: error in apply_autoinstall",
             "curtin: ERROR: partitioning failed",
             "Out of memory: Killed process 1234",
             "blk_update_request: I/O error, dev nvme0n1, sector 0",
             "EXT4-fs error (device dm-0): ext4_lookup",
             "mce: [Hardware Error]: CPU 3: Machine Check"]
    p = tmp_path / "sol.txt"
    p.write_text(CLEAN + "\r\n".join(lines) + "\r\n")
    r = solscan.scan_file(p)
    assert r.verdict == "fail"
    assert r.detail["findings"] == len(lines)
    assert r.detail["counts"] == {"kernel panic": 1, "call trace": 1, "subiquity error": 2,
                                  "curtin error": 1, "out of memory": 1, "I/O error": 1,
                                  "ext4 error": 1, "machine check": 1}
    first = r.detail["rows"][0]
    assert first == ["line 6", "kernel panic: [ 12.1] Kernel panic - not syncing: VFS"]
    assert r.detail["headline"].startswith("9 error line(s) in SOL")


def test_ansi_and_cr_do_not_split_a_match():
    text = "\x1b[1;31mKernel\x1b[0m panic - not syncing\r\nprogress 10%\rEXT4-fs error x\n"
    hits = solscan.scan(text)
    assert [h["pattern"] for h in hits] == ["kernel panic", "ext4 error"]
    assert hits[1]["text"] == "EXT4-fs error x"


def test_case_sensitive_patterns_ignore_benign_words():
    assert solscan.scan("curtin: finished ok\nsubiquity: done\nno errors here\n") == []
    assert solscan.scan("curtin stage failed\n")[0]["pattern"] == "curtin error"


def test_rows_capped_but_counted(tmp_path):
    p = tmp_path / "sol.txt"
    p.write_text("I/O error\n" * (solscan.MAX_ROWS + 7))
    r = solscan.scan_file(p)
    assert r.detail["findings"] == solscan.MAX_ROWS + 7
    assert len(r.detail["rows"]) == solscan.MAX_ROWS
    assert any("7 more" in n for n in r.detail["notes"])


def test_missing_or_empty_capture_is_unmeasured(tmp_path):
    assert solscan.scan_file(tmp_path / "nope.txt").verdict == "unmeasured"
    (tmp_path / "e.txt").write_bytes(b"")
    r = solscan.scan_file(tmp_path / "e.txt")
    assert r.verdict == "unmeasured" and "0 bytes" in r.detail["headline"]


def test_tail(tmp_path):
    p = tmp_path / "sol.txt"
    p.write_bytes(b"x" * 10000 + b"\x1b[0mlast line\r\n")
    assert solscan.tail(p, 20).endswith("last line\n")
    assert len(solscan.tail(p, 20)) == 20
    assert solscan.tail(tmp_path / "nope", 20) == ""


def test_virtual_media_io_errors_and_mce_info_are_not_findings():
    text = ("[  612.2] Buffer I/O error on dev sr0, logical block 0, async page read\n"
            "[  612.3] blk_update_request: I/O error, dev loop3, sector 8\n"
            "[    0.9] mce: CPU0: Thermal monitoring enabled (TM1)\n"
            "[    1.0] mce: [Hardware Error]: Machine check events logged\n"
            "[  700.1] Buffer I/O error on dev sda, logical block 0, async page read\n")
    hits = solscan.scan(text)
    assert [(h["line"], h["pattern"]) for h in hits] == [(4, "machine check"), (5, "I/O error")]
