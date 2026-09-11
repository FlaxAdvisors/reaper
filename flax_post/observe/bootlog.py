# flax_post/observe/bootlog.py
"""Boot-marker scrapers (spec 2026-09-11 post-slot-ladder §6): did this host
IP fetch iPXE over tftp, fetch its boot.ipxe, fetch the LiveLeap ISO — after
`since`? Reads the file from the END in bounded chunks and stops at the first
line older than `since`, so a 23 MB access.log costs a few reads, not a scan.

dnsmasq's `log-facility=<file>` lines look like
  Sep 11 14:31:51 dnsmasq-tftp[53177]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.28.4
(no year, local time). nginx lines carry [11/Sep/2026:14:32:58 +0000].
"""
import datetime
import os
import re
import time

DNSMASQ_LOG = os.environ.get("FLAX_POST_DNSMASQ_LOG", "/var/log/dnsmasq/dnsmasq.log")
NGINX_ACCESS_LOG = os.environ.get("FLAX_POST_NGINX_ACCESS_LOG", "/var/log/nginx/access.log")
_CHUNK = 65536
_MAX_BYTES = 4 * 1024 * 1024
_ISO_NEEDLE = "/suse/live/test/LiveLeap.x86_64-"
_IPXE_RE = re.compile(r'"GET /(?:v\d+/)?boot\.ipxe ')
_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _ip_token(ip):
    return re.compile(r"(?<![\d.])" + re.escape(ip) + r"(?![\d.])")


def _tail_lines(path, max_bytes=_MAX_BYTES, chunk=_CHUNK):
    """Yield complete lines newest-first, reading at most max_bytes from the end."""
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        read_total = 0
        buf = b""
        while pos > 0 and read_total < max_bytes:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            read_total += step
            lines = buf.split(b"\n")
            buf = lines[0]                       # possibly partial first line
            for ln in reversed(lines[1:]):
                if ln:
                    yield ln.decode("utf-8", "replace")
        if pos == 0 and buf:
            yield buf.decode("utf-8", "replace")


def _nginx_ts(line):
    lb, rb = line.find("["), line.find("]")
    if lb < 0 or rb < lb:
        return None
    try:
        return datetime.datetime.strptime(line[lb + 1:rb], "%d/%b/%Y:%H:%M:%S %z").timestamp()
    except ValueError:
        return None


def _syslog_ts(line, ref_epoch):
    """'Sep 11 14:31:51 ...' -> epoch, local time, year taken from ref_epoch
    (rolled back one year if the result lands more than two days in the future)."""
    try:
        parts = line.split(None, 3)
        mon, day, clock = parts[0], parts[1], parts[2]
        h, m, s = clock.split(":")
        year = datetime.datetime.fromtimestamp(ref_epoch).year
        t = time.mktime((year, _MONTHS[mon], int(day), int(h), int(m), int(s), 0, 0, -1))
        if t > ref_epoch + 86400 * 2:
            t = time.mktime((year - 1, _MONTHS[mon], int(day), int(h), int(m), int(s), 0, 0, -1))
        return t
    except (ValueError, KeyError, IndexError):
        return None


def _scan(path, since, match, ts_of, max_bytes):
    for line in _tail_lines(path, max_bytes=max_bytes):
        ts = ts_of(line)
        if ts is None:
            continue
        if ts < since:
            return None
        if match(line):
            return ts
    return None


def tftp_seen(path, host_ip, since, *, max_bytes=_MAX_BYTES):
    tok = _ip_token(host_ip)
    def match(line):
        return " sent /" in line and " to " in line and tok.search(line.rsplit(" to ", 1)[-1]) is not None
    return _scan(path, since, match, lambda ln: _syslog_ts(ln, time.time()), max_bytes)


def ipxe_seen(path, host_ip, since, *, max_bytes=_MAX_BYTES):
    def match(line):
        return line.startswith(host_ip + " ") and _IPXE_RE.search(line) \
            and "iPXE" in line
    return _scan(path, since, match, _nginx_ts, max_bytes)


def iso_seen(path, host_ip, since, *, max_bytes=_MAX_BYTES):
    def match(line):
        return line.startswith(host_ip + " ") and _ISO_NEEDLE in line
    return _scan(path, since, match, _nginx_ts, max_bytes)
