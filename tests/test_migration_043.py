"""Migration 043: post_fio_drive (post lane, single writer flax_post)."""
import importlib.util
import pathlib

import flax_post

ROOT = pathlib.Path(flax_post.__file__).resolve().parent.parent


def _load():
    p = ROOT / "schema" / "versions" / "043_post_fio_drive.py"
    spec = importlib.util.spec_from_file_location("m043", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sql(m, monkeypatch, fn="upgrade"):
    seen = []
    monkeypatch.setattr(m.op, "execute", lambda sql: seen.append(sql))
    getattr(m, fn)()
    return " ".join(" ".join(seen).lower().split())


def test_043_static(monkeypatch):
    m = _load()
    assert (m.revision, m.down_revision) == ("043000000001", "042000000001")
    assert m.__doc__.startswith("post:")
    up = _sql(m, monkeypatch)
    assert "create table if not exists post_fio_drive" in up
    assert "unique (bmc_mac, run_id, dev, job, metric)" in up
    assert "grant select, insert, update, delete on post_fio_drive to flax_post" in up
    assert up.count(" to flax_") == 2  # the table + its sequence, flax_post only
    assert "drop table if exists post_fio_drive" in _sql(m, monkeypatch, "downgrade")
