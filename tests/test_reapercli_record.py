"""Deploy records: /etc/flax/deployed/<component>.json on each target bang (spec §8.5)."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.lib.inventory import site_hosts  # noqa: E402
from reapercli.lib.record import WRITER, make_record, write_records  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EHV = site_hosts(load_sites(ROOT)["eindhoven"], ROOT, "orbit")
T = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def test_record_shape():
    r = make_record("flax_post", "a" * 40, "flax-control:0.9.207", "dbahi", "bang-gouda", T)
    assert r == {"component": "flax_post", "commit": "a" * 40, "image": "flax-control:0.9.207",
                 "user": "dbahi", "from": "bang-gouda", "at": "2026-11-08T12:00:00Z"}


def test_write_one_call_per_bang():
    calls = []

    def runner(argv, stdin, timeout):
        calls.append((argv, stdin))
        return Result(0, "", "")
    recs = [make_record(c, "a" * 40, None, "u", "h", T) for c in ("flax_post", "post_tgz")]
    res = write_records(EHV, ["bang-gouda", "bang-edam"], recs, runner)
    assert set(res) == {"bang-gouda", "bang-edam"} and len(calls) == 2
    for argv, stdin in calls:
        assert json.loads(stdin) == recs
        assert "/etc/flax/deployed" in argv[-1]


def test_writer_roundtrip(tmp_path):
    recs = [make_record("flax_post", "b" * 40, None, "u", "h", T)]
    subprocess.run([sys.executable, "-c", WRITER, str(tmp_path)], input=json.dumps(recs), text=True, check=True)
    got = {p.stem: json.loads(p.read_text()) for p in tmp_path.glob("*.json")}
    assert got == {"flax_post": recs[0]}
    assert oct((tmp_path / "flax_post.json").stat().st_mode & 0o777) == "0o644"
