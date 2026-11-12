"""Tests for flax_observe.vendor_export: the /etc/flax/bmc_vendor.json store
ghost's host-side bins read (Task 4b). Sole writer is flax-observe; the file
is a docker single-file bind mount, so writes are in-place truncate+write,
mirroring flax_post/fwd/store.py's rationale (os.replace over a bind mount
fails EBUSY).
"""
import json
import os

import pytest

from flax_observe import vendor_export as ve


def _store_path(tmp_path):
    return str(tmp_path / "bmc_vendor.json")


def test_update_writes_row_with_six_fields_and_no_creds(tmp_path):
    p = _store_path(tmp_path)
    store = ve.VendorExport(path=p)
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")

    on_disk = json.loads(open(p).read())
    row = on_disk["rabbit-lorax:et6b1"]
    assert set(row) == {"switch", "port", "vendor", "bmc_ip", "bmc_mac",
                        "updated_at"}
    assert row["switch"] == "rabbit-lorax"
    assert row["port"] == "et6b1"
    assert row["vendor"] == "phosphor"
    assert row["bmc_ip"] == "172.24.6.101"
    assert row["bmc_mac"] == "52:54:00:11:22:35"
    assert "creds_used" not in row
    assert "creds_used" not in json.dumps(on_disk)


def test_two_identical_updates_write_file_once(tmp_path, monkeypatch):
    p = _store_path(tmp_path)
    store = ve.VendorExport(path=p)

    calls = []
    real_write = ve._write

    def counting_write(path, data):
        calls.append(1)
        return real_write(path, data)

    monkeypatch.setattr(ve, "_write", counting_write)

    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    assert len(calls) == 1


def test_vendor_change_rewrites_the_row(tmp_path, monkeypatch):
    p = _store_path(tmp_path)
    store = ve.VendorExport(path=p)

    calls = []
    real_write = ve._write

    def counting_write(path, data):
        calls.append(1)
        return real_write(path, data)

    monkeypatch.setattr(ve, "_write", counting_write)

    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    store.update("rabbit-lorax", "et6b1", vendor="ami_legacy",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    assert len(calls) == 2
    on_disk = json.loads(open(p).read())
    assert on_disk["rabbit-lorax:et6b1"]["vendor"] == "ami_legacy"


def test_bmc_mac_falsy_removes_the_row(tmp_path):
    p = _store_path(tmp_path)
    store = ve.VendorExport(path=p)
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip=None, bmc_mac=None)
    on_disk = json.loads(open(p).read())
    assert "rabbit-lorax:et6b1" not in on_disk


def test_vendor_none_with_existing_row_keeps_it(tmp_path):
    p = _store_path(tmp_path)
    store = ve.VendorExport(path=p)
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    store.update("rabbit-lorax", "et6b1", vendor=None,
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    on_disk = json.loads(open(p).read())
    assert on_disk["rabbit-lorax:et6b1"]["vendor"] == "phosphor"


def test_construction_loads_existing_file(tmp_path):
    p = _store_path(tmp_path)
    with open(p, "w") as f:
        json.dump({"rabbit-lorax:et6b1": {
            "switch": "rabbit-lorax", "port": "et6b1", "vendor": "phosphor",
            "bmc_ip": "172.24.6.101", "bmc_mac": "52:54:00:11:22:35",
            "updated_at": "2026-09-18T00:00:00Z",
        }}, f)
    store = ve.VendorExport(path=p)
    # vendor=None with existing row loaded from disk -> the loaded row stands
    store.update("rabbit-lorax", "et6b1", vendor=None,
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    on_disk = json.loads(open(p).read())
    assert on_disk["rabbit-lorax:et6b1"]["vendor"] == "phosphor"


def test_construction_missing_file_is_empty_and_does_not_raise(tmp_path):
    p = str(tmp_path / "nope.json")
    store = ve.VendorExport(path=p)
    assert store._store == {}


def test_construction_malformed_file_is_empty_and_does_not_raise(tmp_path):
    p = _store_path(tmp_path)
    with open(p, "w") as f:
        f.write("not json{{{")
    store = ve.VendorExport(path=p)
    assert store._store == {}


def test_write_failure_in_nonexistent_dir_does_not_raise(tmp_path):
    p = str(tmp_path / "nonexistent-dir" / "bmc_vendor.json")
    store = ve.VendorExport(path=p)
    # Must not raise even though the parent dir does not exist.
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    assert not os.path.exists(p)


def test_same_port_on_two_switches_yields_two_rows(tmp_path):
    p = _store_path(tmp_path)
    store = ve.VendorExport(path=p)
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    store.update("rabbit-gouda", "et6b1", vendor="ami_legacy",
                 bmc_ip="172.24.7.101", bmc_mac="52:54:00:11:22:99")
    on_disk = json.loads(open(p).read())
    assert set(on_disk) == {"rabbit-lorax:et6b1", "rabbit-gouda:et6b1"}


def test_file_is_written_in_place_inode_unchanged(tmp_path):
    p = _store_path(tmp_path)
    store = ve.VendorExport(path=p)
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    ino1 = os.stat(p).st_ino
    store.update("rabbit-lorax", "et6b1", vendor="ami_legacy",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")
    ino2 = os.stat(p).st_ino
    assert ino1 == ino2


def test_remove_drops_row_and_writes_only_if_it_existed(tmp_path, monkeypatch):
    p = _store_path(tmp_path)
    store = ve.VendorExport(path=p)
    store.update("rabbit-lorax", "et6b1", vendor="phosphor",
                 bmc_ip="172.24.6.101", bmc_mac="52:54:00:11:22:35")

    calls = []
    real_write = ve._write

    def counting_write(path, data):
        calls.append(1)
        return real_write(path, data)

    monkeypatch.setattr(ve, "_write", counting_write)

    store.remove("rabbit-lorax", "et6b1")
    assert len(calls) == 1
    on_disk = json.loads(open(p).read())
    assert "rabbit-lorax:et6b1" not in on_disk

    # removing an already-absent row is a no-op (no extra write)
    store.remove("rabbit-lorax", "et6b1")
    assert len(calls) == 1
