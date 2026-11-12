"""Stale single-file bind mounts (memory single-file-bind-mounts-go-stale): a template or
copy write is a rename, so the host path gets a NEW inode while a running container keeps
the one it mounted. Stale = host inode != the inode the container sees. (ctime was tried
first and was wrong: in-place writes bump ctime but keep the inode -- 5 of 6 false
positives on bang-gouda 2026-11-08; only the console's bmcfw.json was really stale.)"""
import json

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import main  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.stale import restart_hint, stale_binds  # noqa: E402


def facts(state="running", **inodes):
    """inodes: name=(host_ino, container_ino)"""
    return {"containers": {"flax-observe": {"state": state}},
            "bind_inodes": {"flax-observe": {f"/etc/flax/{k}.json": list(v) for k, v in inodes.items()}}}


def test_stale_when_host_inode_differs():
    assert stale_binds(facts(geometry=(2, 1), vlans=(5, 5))) == [("flax-observe", "/etc/flax/geometry.json")]


def test_in_place_write_is_not_stale():
    assert stale_binds(facts(post_fw=(7, 7))) == []


def test_unknown_inode_is_not_stale():
    assert stale_binds(facts(geometry=(2, None))) == []


def test_stopped_container_ignored():
    assert stale_binds(facts(state="exited", geometry=(2, 1))) == []


def test_error_or_dormant_facts():
    assert stale_binds({"error": "timeout"}) == [] and stale_binds({"dormant": True}) == []
    assert stale_binds({"containers": {}, "bind_inodes": {"error": "sudo: a password is required"}}) == []


def test_restart_hint():
    assert restart_hint("eindhoven", "bang-gouda", "flax-observe") == \
        "reaper svc restart eindhoven flax-observe --host bang-gouda --yes"


def test_status_shows_stale_row(tmp_path, capsys):
    from test_reapercli_status import bang_facts, ctx_for

    def runner(argv, stdin, timeout):
        f = bang_facts("flax@bang-gouda" in argv, "flax@bang-gouda" not in argv)
        if "flax@bang-gouda" in argv:
            f.update(facts(geometry=(2, 1)))
        return Result(0, json.dumps(f), "")
    main(["status", "eindhoven"], ctx_for(tmp_path, "eindhoven", ssh=runner))
    rows = [l for l in capsys.readouterr().out.splitlines() if "stale-bind" in l]
    assert len(rows) == 1 and "warn" in rows[0] and "geometry.json" in rows[0] and "bang-gouda" in rows[0]
