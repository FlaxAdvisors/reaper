import json
import os
import stat
import subprocess

SCRIPT = os.path.join(os.path.dirname(__file__), "..",
                      "scripts", "kea-bmc-denials.sh")


def _stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write("#!/bin/bash\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path, journal_body, args=None):
    d = str(tmp_path)
    _stub(d, "journalctl", journal_body)
    env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
    return subprocess.run(["bash", os.path.abspath(SCRIPT)] + (args or []),
                          env=env, capture_output=True, text=True, timeout=30)


def test_two_denials_one_mac_counted(tmp_path):
    body = (
        'echo "ALLOC_ENGINE_V4_ALLOC_FAIL_CLASSES ... [hwtype=1 98:03:9b:75:3c:32] ..."\n'
        'echo "some unrelated line"\n'
        'echo "ALLOC_ENGINE_V4_ALLOC_FAIL_CLASSES ... [hwtype=1 98:03:9b:75:3c:32] ..."\n'
    )
    r = _run(tmp_path, body, ["5"])
    assert r.returncode == 0
    assert json.loads(r.stdout.strip()) == {
        "window_min": 5, "denials": 2, "by_mac": {"98:03:9b:75:3c:32": 2}
    }


def test_no_matches_yields_zero_denials(tmp_path):
    body = 'echo "some unrelated line"\n'
    r = _run(tmp_path, body, ["5"])
    assert r.returncode == 0
    assert json.loads(r.stdout.strip()) == {"window_min": 5, "denials": 0, "by_mac": {}}


def test_default_window_is_60_minutes(tmp_path):
    r = _run(tmp_path, 'echo "no match here"\n')
    assert r.returncode == 0
    assert json.loads(r.stdout.strip())["window_min"] == 60


def test_bad_minutes_arg_is_usage_error(tmp_path):
    r = _run(tmp_path, 'echo "no match here"\n', ["nope"])
    assert r.returncode == 2
    assert "usage" in r.stderr
