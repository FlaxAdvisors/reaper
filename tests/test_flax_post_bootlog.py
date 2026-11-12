"""observe/bootlog: tail-read matchers for the bang's tftp + nginx logs.
Fixture lines are verbatim from bang-gouda on 2026-09-11 (IPs changed)."""
import calendar
import time
from flax_post.observe import bootlog

NGINX = """172.25.28.4 - - [11/Sep/2026:14:32:58 +0000] "GET /boot.ipxe HTTP/1.1" 200 47 "-" "iPXE/1.21.1+ (gc1834)"
172.25.28.4 - - [11/Sep/2026:14:32:58 +0000] "GET /v25/boot.ipxe HTTP/1.1" 200 23877 "-" "iPXE/1.21.1+ (gc1834)"
172.25.28.40 - - [11/Sep/2026:14:33:00 +0000] "GET /v25/boot.ipxe HTTP/1.1" 200 23877 "-" "iPXE/1.21.1+ (gc1834)"
172.25.28.4 - - [11/Sep/2026:14:33:25 +0000] "GET /install//suse/live/test/initrd-3.0.0 HTTP/1.1" 200 159446936 "-" "iPXE/1.21.1+ (gc1834)"
172.25.28.4 - - [11/Sep/2026:14:34:09 +0000] "GET /install//suse/live/test/LiveLeap.x86_64-3.0.0.iso HTTP/1.1" 200 2861563904 "-" "curl/8.12.1"
10.0.0.9 - - [11/Sep/2026:14:35:00 +0000] "GET /api/v1/blades HTTP/1.1" 200 100 "-" "curl/8.12.1"
"""
DNSMASQ = """Sep 11 14:31:51 dnsmasq-tftp[53177]: error 8 User aborted the transfer received from 172.25.28.4
Sep 11 14:31:51 dnsmasq-tftp[53177]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.28.4
Sep 11 14:31:52 dnsmasq-tftp[53177]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.28.40
Sep 11 14:31:53 dnsmasq-tftp[53177]: file /srv/tftpboot/efi64/autoexec.ipxe not found for 172.25.28.4
"""


def _epoch(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_ipxe_seen_matches_whole_ip_token_and_ipxe_ua(tmp_path):
    p = _write(tmp_path, "access.log", NGINX)
    since = _epoch("2026-09-11 14:30:00")
    assert bootlog.ipxe_seen(p, "172.25.28.4", since) == _epoch("2026-09-11 14:32:58")
    assert bootlog.ipxe_seen(p, "172.25.28.40", since) == _epoch("2026-09-11 14:33:00")
    assert bootlog.ipxe_seen(p, "172.25.28.41", since) is None


def test_iso_seen_returns_completion_line_time(tmp_path):
    p = _write(tmp_path, "access.log", NGINX)
    assert bootlog.iso_seen(p, "172.25.28.4", _epoch("2026-09-11 14:30:00")) == _epoch("2026-09-11 14:34:09")


def test_boundary_excludes_a_prior_boot(tmp_path):
    p = _write(tmp_path, "access.log", NGINX)
    assert bootlog.iso_seen(p, "172.25.28.4", _epoch("2026-09-11 14:34:10")) is None


def test_tftp_seen_from_dnsmasq_syslog_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC"); time.tzset()
    p = _write(tmp_path, "dnsmasq.log", DNSMASQ)
    since = _epoch("2026-09-11 14:30:00")
    assert bootlog.tftp_seen(p, "172.25.28.4", since) == _epoch("2026-09-11 14:31:51")
    assert bootlog.tftp_seen(p, "172.25.28.40", since) == _epoch("2026-09-11 14:31:52")
    assert bootlog.tftp_seen(p, "172.25.28.4", _epoch("2026-09-11 14:31:52")) is None


def test_tftp_seen_handles_padded_day(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC"); time.tzset()
    padded = "Sep  1 04:11:23 dnsmasq-tftp[53177]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.28.4\n"
    p = _write(tmp_path, "dnsmasq.log", padded)
    assert bootlog.tftp_seen(p, "172.25.28.4", _epoch("2026-09-01 04:00:00")) == _epoch("2026-09-01 04:11:23")


def test_missing_file_is_none(tmp_path):
    assert bootlog.tftp_seen(str(tmp_path / "nope"), "1.2.3.4", 0) is None


def test_tail_reads_across_chunk_boundary(tmp_path):
    filler = ("10.0.0.9 - - [11/Sep/2026:14:35:00 +0000] \"GET /x HTTP/1.1\" 200 1 \"-\" \"curl\"\n") * 3000
    p = _write(tmp_path, "access.log", NGINX + filler)
    assert bootlog.iso_seen(p, "172.25.28.4", _epoch("2026-09-11 14:30:00")) == _epoch("2026-09-11 14:34:09")


TWO_BOOTS_DNSMASQ = """Sep 14 06:50:52 dnsmasq-tftp[2771]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.24.2
Sep 14 06:50:52 dnsmasq-tftp[2771]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.24.2
Sep 14 07:10:00 dnsmasq-tftp[2771]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.24.20
Sep 14 08:44:01 dnsmasq-tftp[2771]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.24.2
"""
TWO_BOOTS_NGINX = """172.25.24.2 - - [14/Sep/2026:06:52:00 +0000] "GET /boot.ipxe HTTP/1.1" 200 47 "-" "iPXE/1.21.1+ (gc1834)"
172.25.24.2 - - [14/Sep/2026:06:53:10 +0000] "GET /install//suse/live/test/LiveLeap.x86_64-3.0.0.iso HTTP/1.1" 200 2861563904 "-" "curl/8.12.1"
172.25.24.20 - - [14/Sep/2026:07:11:00 +0000] "GET /boot.ipxe HTTP/1.1" 200 47 "-" "iPXE/1.21.1+ (gc1834)"
172.25.24.2 - - [14/Sep/2026:08:45:08 +0000] "GET /boot.ipxe.cfg HTTP/1.1" 200 1300 "-" "iPXE/1.21.1+ (gc1834)"
172.25.24.2 - - [14/Sep/2026:08:45:08 +0000] "GET /boot.ipxe HTTP/1.1" 200 47 "-" "iPXE/1.21.1+ (gc1834)"
172.25.24.2 - - [14/Sep/2026:08:45:08 +0000] "GET /v25/boot.ipxe HTTP/1.1" 200 23867 "-" "iPXE/1.21.1+ (gc1834)"
172.25.24.2 - - [14/Sep/2026:08:46:20 +0000] "GET /install//suse/live/test/LiveLeap.x86_64-3.0.0.iso HTTP/1.1" 200 2861563904 "-" "curl/8.12.1"
"""


def test_boot_times_lists_every_marker_since_in_time_order(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC"); time.tzset()
    d = _write(tmp_path, "dnsmasq.log", TWO_BOOTS_DNSMASQ)
    n = _write(tmp_path, "access.log", TWO_BOOTS_NGINX)
    got = bootlog.boot_times(d, n, "172.25.24.2", _epoch("2026-09-14 06:00:00"))
    assert got == {"tftp": [_epoch("2026-09-14 06:50:52"), _epoch("2026-09-14 08:44:01")],
                   "ipxe": [_epoch("2026-09-14 06:52:00"), _epoch("2026-09-14 08:45:08")],
                   "iso": [_epoch("2026-09-14 06:53:10"), _epoch("2026-09-14 08:46:20")]}
    got = bootlog.boot_times(d, n, "172.25.24.2", _epoch("2026-09-14 07:00:00"))
    assert got == {"tftp": [_epoch("2026-09-14 08:44:01")], "ipxe": [_epoch("2026-09-14 08:45:08")],
                   "iso": [_epoch("2026-09-14 08:46:20")]}


def test_boot_times_missing_files_are_empty(tmp_path):
    assert bootlog.boot_times(str(tmp_path / "a"), str(tmp_path / "b"), "1.2.3.4", 0) == \
        {"tftp": [], "ipxe": [], "iso": []}


def test_tail_stops_at_max_bytes(tmp_path):
    filler = ("10.0.0.9 - - [11/Sep/2026:14:35:00 +0000] \"GET /x HTTP/1.1\" 200 1 \"-\" \"curl\"\n") * 3000
    p = _write(tmp_path, "access.log", NGINX + filler)
    # the match is older than the newest 64 KB -> not found when capped there
    assert bootlog.iso_seen(p, "172.25.28.4", _epoch("2026-09-11 14:30:00"), max_bytes=65536) is None
