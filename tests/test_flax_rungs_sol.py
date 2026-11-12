"""flax_rungs.sol: rackpost's console capture, read from the shared flax-post-sol relay."""
import os
from pathlib import Path

import pytest

pytest.importorskip("flax_rungs.sol")
from flax_rungs import sol


class Relay:
    """The relay's capture dir as flax_post_sol/src/sessions/capture.ts keeps it:
    <ip>.txt appended by the session, mark (and the size cap) rename it to .prev.txt
    and start a new file with a header line."""

    def __init__(self, d: Path, ip="172.32.37.11"):
        self.d, self.ip, self.marks = d, ip, []
        self.cur, self.prev = d / f"{ip}.txt", d / f"{ip}.prev.txt"

    def write(self, b: bytes):
        with open(self.cur, "ab") as f:
            f.write(b)

    def rotate(self, header: bytes):
        if self.cur.exists():
            os.replace(self.cur, self.prev)
        self.cur.write_bytes(header)

    def mark(self, ip, reason):
        self.marks.append((ip, reason))
        self.rotate(f"=== mark {reason} ===\n".encode())
        return {"ok": True, "ip": ip}


def _start(relay, path, **kw):
    return sol.start(relay.ip, path, capture_dir=relay.d, mark_fn=relay.mark, **kw)


def test_capture_is_the_relay_file_from_the_mark(tmp_path):
    relay = Relay(tmp_path)
    relay.write(b"older boot, not ours\n")
    path = tmp_path / "artifacts" / "L37" / "q9" / "console" / "sol.txt"
    cap = _start(relay, path, reason="rackpost-qualify")
    relay.write(b"POST 0xB2\nBooting `flax live`\n")
    r = cap.stop()
    assert relay.marks == [("172.32.37.11", "rackpost-qualify")]
    assert path.read_bytes() == b"=== mark rackpost-qualify ===\nPOST 0xB2\nBooting `flax live`\n"
    assert r.verdict == "pass" and r.detail["bytes"] == path.stat().st_size
    assert r.detail["rotations"] == 0 and "flax-post-sol" in r.detail["source"]
    assert cap.stop() is r                  # idempotent


def test_artifact_grows_while_the_stage_runs(tmp_path):
    relay = Relay(tmp_path)
    path = tmp_path / "sol.txt"
    cap = _start(relay, path)
    relay.write(b"installer line\n")
    cap.sync()                              # what the copy thread does every SYNC_S
    assert path.read_bytes().endswith(b"installer line\n")
    relay.write(b"more\n")
    cap.stop()
    assert path.read_bytes().endswith(b"installer line\nmore\n")


def test_rotation_between_syncs_keeps_the_old_file_tail(tmp_path):
    relay = Relay(tmp_path)
    path = tmp_path / "sol.txt"
    cap = _start(relay, path)
    relay.write(b"a\n")
    cap.sync()
    relay.write(b"b before the size cap\n")
    relay.rotate(b"=== rotated size-cap ===\n")      # relay's cap, or another mark
    relay.write(b"c\n")
    r = cap.stop()
    assert path.read_bytes() == (b"=== mark rackpost ===\na\nb before the size cap\n"
                                 b"=== rotated size-cap ===\nc\n")
    assert r.detail["rotations"] == 1 and "note" not in r.detail


def test_two_rotations_reusing_the_inode_number_are_seen_by_size(tmp_path):
    # two rotations inside one SYNC_S free the followed inode and the new <ip>.txt may
    # get its number back: the same inode, but shorter than what was copied
    relay = Relay(tmp_path)
    path = tmp_path / "sol.txt"
    cap = _start(relay, path)
    relay.write(b"a long first boot line\n")
    cap.sync()
    ino = relay.cur.stat().st_ino
    relay.cur.write_bytes(b"=== mark x ===\nz\n")     # same inode, new short content
    assert relay.cur.stat().st_ino == ino
    r = cap.stop()
    assert path.read_bytes().endswith(b"a long first boot line\n=== mark x ===\nz\n")
    assert r.detail["rotations"] == 1 and "outran the copy" in r.detail["note"]


def test_relay_down_follows_from_the_current_end(tmp_path):
    relay = Relay(tmp_path)
    relay.write(b"old\n")
    path = tmp_path / "sol.txt"
    cap = sol.start(relay.ip, path, capture_dir=relay.d,
                    mark_fn=lambda ip, why: {"ok": False, "reason": "connection refused"})
    relay.write(b"new\n")
    r = cap.stop()
    assert path.read_bytes() == b"new\n" and r.verdict == "pass"
    assert "relay mark failed (connection refused)" in r.detail["note"]


def test_nothing_captured_fails(tmp_path):
    cap = sol.start("10.9.9.9", tmp_path / "sol.txt", capture_dir=tmp_path,
                    mark_fn=lambda ip, why: {"ok": False, "reason": "connection refused"})
    r = cap.stop()
    assert r.verdict == "fail" and r.detail["error"].startswith("no sol capture (0 bytes")
    assert "connection refused" in r.detail["error"]


def test_tail_follows_offsets_and_resets_on_rotation(tmp_path):
    relay = Relay(tmp_path)
    assert sol.tail(relay.ip, capture_dir=tmp_path)["ino"] is None
    relay.write(b"x" * 100 + b"\nline\n")
    t = sol.tail(relay.ip, capture_dir=tmp_path, max_bytes=8)
    assert t["reset"] and t["text"] == "xx\nline\n"          # the last max_bytes
    relay.write(b"next\n")
    t2 = sol.tail(relay.ip, offset=t["offset"], ino=t["ino"], capture_dir=tmp_path)
    assert not t2["reset"] and t2["text"] == "next\n"
    relay.rotate(b"=== mark ===\n")
    t3 = sol.tail(relay.ip, offset=t2["offset"], ino=t2["ino"], capture_dir=tmp_path)
    assert t3["reset"] and t3["text"] == "=== mark ===\n"


def test_rackpost_opens_no_sol_session_of_its_own():
    src = Path(sol.__file__).read_text()
    assert "ipmitool" not in src.split('"""', 2)[2] and "import subprocess" not in src
