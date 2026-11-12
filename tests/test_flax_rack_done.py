import csv, io
import pytest
pytest.importorskip("flax_rack.done")
from flax_rack import done
from flax_rack.deploy import run_deploy
from flax_rack.report import DEVICE_COLUMNS, FIRMWARE_COLUMNS
from flax_rungs.result import RungResult
from tests.test_flax_rack_deploy import DFactory, calls, det, events, qualified, v
from tests.test_flax_rack_qualify import profiles  # noqa: F401 -- fixture
from tests.test_flax_rack_server import _order_dir, _start, _wait_idle, get, get_json, post

HOSTS = ("L37", "C37", "R37", "L05", "R05")


@pytest.fixture
def ctx(tmp_path, profiles):
    """Discover + Qualify + Deploy on FakeRungs; L37's first boot was refused, so it has
    no installed OS."""
    c = qualified(tmp_path)
    c.rungs.firstboot_fail = {"L37"}
    run_deploy(c)
    c.rungs.calls.clear()
    return c


def rows(path):
    return list(csv.DictReader(open(path, newline="")))


def test_done_skips_inband_without_firstboot(ctx):
    out = done.run(ctx)
    assert sorted(out["devices"]) == sorted(HOSTS)       # host-bearing only, no JBOD
    es = {c[1]: c[2] for c in calls(ctx, "end_summary")}
    assert es["L37"] is None and es["C37"] == "172.32.37.51"
    d = det(ctx, "L37", "end-summary")
    assert ["in-band", "skipped: no installed OS to reach"] in d["rows"]
    assert v(ctx, "L37", "end-summary") == "pass" and d["inband"] is False
    lr = ctx.db.latest_runs()
    assert "sel-elist" in lr[("L37", "end-summary")]["artifacts"]          # OOB still read
    assert "disks" not in lr[("L37", "end-summary")]["artifacts"]
    assert {"disks", "bootorder", "net", "dimms", "sel-elist", "sdr-elist"} <= \
        set(lr[("C37", "end-summary")]["artifacts"])
    rel = lr[("C37", "end-summary")]["artifacts"]["sel-elist"]
    assert rel.startswith("artifacts/C37/done") and rel.endswith("/end-summary/sel-elist.txt")
    assert lr[("C37", "end-summary")]["stage"] == "done"
    # components from the Qualify inventory, and the CSV rows (L37's deploy is a fail)
    assert v(ctx, "L37", "components") == "pass" and ctx.db.components("L37")
    devs = {r["pos"]: r for r in rows(out["csv"]["firmware"])}
    assert devs["L37"]["deploy"] == "fail" and devs["L37"]["done"] == "pass"
    assert devs["C37"]["done"] == "pass" and "JBOD01" in devs
    assert events(ctx)[0] == f"csv written ({len(devs)} devices): F4-RRORD-101-devices.csv, " \
        "F4-RRORD-101-firmware.csv, F4-RRORD-101-inventory.csv"
    assert sorted(out["csv"]) == ["devices", "firmware", "inventory"]


def test_sel_nonbenign_fails_end_summary(ctx):
    ctx.rungs.sel_nonbenign = {"R37"}
    out = done.run(ctx)
    assert v(ctx, "R37", "end-summary") == "fail"
    assert "Correctable ECC" in det(ctx, "R37", "end-summary")["sel_nonbenign"][0]
    assert {r["pos"]: r["done"] for r in rows(out["csv"]["firmware"])}["R37"] == "fail"
    assert "not clean: R37 (end-summary)" in events(ctx)[1]


def test_boot_reset_removes_hostname_file(ctx, tmp_path):
    pxe = tmp_path / "pxe"; pxe.mkdir()
    ctx.rungs.pxe_dir = str(pxe)
    (pxe / "hostname-rr101-c37.ipxe").write_text("#!ipxe\n")
    (pxe / "hostname-rr101-r37.ipxe").write_text("#!ipxe\n")      # not in this run: kept
    done.run(ctx, ["C37"])
    assert not (pxe / "hostname-rr101-c37.ipxe").exists()
    assert (pxe / "hostname-rr101-r37.ipxe").exists()
    assert v(ctx, "C37", "boot-reset") == "pass"
    assert calls(ctx, "clear_override") == [("clear_override", "C37")]
    # never a power action
    assert not [c for c in ctx.rungs.calls if c[0] in ("power_action", "install", "firstboot")]
    ctx.rungs.override_fail = {"C37"}
    done.run(ctx, ["C37"])
    assert v(ctx, "C37", "boot-reset") == "fail"
    assert "boot override NOT cleared: http 400" in det(ctx, "C37", "boot-reset")["error"]


def test_boot_reset_fails_when_bootorder_starts_with_pxe(ctx):
    ctx.rungs.pxe_first = {"C37"}
    done.run(ctx, ["C37", "R37"])
    assert v(ctx, "C37", "boot-reset") == "fail"
    assert "NETWORK FIRST: Boot0001* UEFI: PXE" in det(ctx, "C37", "boot-reset")["error"]
    assert ("clear_override", "C37") in calls(ctx, "clear_override")   # cleared anyway
    assert v(ctx, "R37", "boot-reset") == "pass"
    assert ["boot order", "first Boot0003* ubuntu\tHD(1,GPT,...)/File(\\EFI\\ubuntu\\shimx64.efi)"] \
        in det(ctx, "R37", "boot-reset")["rows"]


def test_components_skip_without_inventory_drops_old_rows(ctx):
    done.run(ctx, ["R05"])
    assert ctx.db.components("R05")
    ctx.db.finish_run(ctx.db.start_run("R05", "qualify", "inventory"),
                      RungResult("fail", {"error": "agent gone"}))
    done.run(ctx, ["R05"])
    assert v(ctx, "R05", "components") == "skip"
    assert det(ctx, "R05", "components")["reason"] == "no Qualify inventory"
    assert ctx.db.components("R05") == []


def test_cancel_between_devices(ctx, monkeypatch):
    real = ctx.rungs.end_summary

    def es(*a, **k):
        ctx.cancel.set()                    # pressed while the first device is read
        return real(*a, **k)
    ctx.rungs.end_summary = es
    monkeypatch.setattr(done, "MAX_PARALLEL", 1)
    out = done.run(ctx)
    (first,) = [c[1] for c in calls(ctx, "end_summary")]
    assert v(ctx, first, "end-summary") == "pass"
    assert det(ctx, first, "boot-reset")["reason"] == "cancelled"
    for pos in HOSTS:
        if pos != first:
            for rung in done.DONE_RUNGS:
                assert det(ctx, pos, rung)["reason"] == "cancelled", (pos, rung)
    assert out["csv"] is None and not (ctx.dir / "F4-RRORD-101-devices.csv").exists()
    assert not calls(ctx, "clear_override")
    assert events(ctx)[0] == "csv not written (cancelled)"


# ---------------------------------------------------------------- server
@pytest.fixture
def srv(tmp_path, profiles):
    _order_dir(tmp_path)
    s = _start(tmp_path, DFactory())
    yield s
    s.shutdown(); s.server_close()


def test_rack_wide_needs_typed_confirm(srv):
    u = "/rr101/api/stage/done"
    for body in ({}, {"confirm": "rrord-101"}, {"confirm": True}):
        r = post(srv, u, body)
        assert r.status == 400 and b"order id" in r.body, body
    assert post(srv, u, {"positions": ["C37"]}).status == 400
    r = post(srv, u, {"positions": ["JBOD01"], "confirm": True})
    assert r.status == 400 and b"not a host-bearing position: JBOD01" in r.body
    s = get_json(srv, "/rr101/api/state")
    assert s["done_rungs"] == list(done.DONE_RUNGS) and s["done"]["csv_at"] is None
    assert {d["pos"]: d["done_rungs"] for d in s["devices"]}["JBOD01"] == []
    r = post(srv, u, {"positions": ["C37"], "confirm": True})
    assert r.status == 202
    _wait_idle(srv)
    r = post(srv, u, {"confirm": "RRORD-101"})
    assert r.status == 202
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    assert s["done"]["csv_at"]
    c37 = next(d for d in s["devices"] if d["pos"] == "C37")
    # nothing discovered/qualified here: OOB only, no inventory
    assert c37["runs"]["components"]["verdict"] == "skip"
    assert c37["runs"]["end-summary"]["artifacts"]["sel-elist"].endswith("sel-elist.txt")


def test_csv_served_after_done(srv):
    for n in ("devices.csv", "firmware.csv", "inventory.csv"):
        assert get(srv, f"/rr101/api/report/{n}").status == 404
    assert get(srv, "/rr101/api/report/order.db").status == 404
    assert post(srv, "/rr101/api/stage/done", {"confirm": "RRORD-101"}).status == 202
    _wait_idle(srv)
    r = get(srv, "/rr101/api/report/devices.csv")
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/csv")
    assert r.headers["Content-Disposition"] == \
        'attachment; filename="F4-RRORD-101-devices.csv"'
    # the order has no rack tag: no rack column
    assert next(csv.reader(io.StringIO(r.body.decode()))) == list(DEVICE_COLUMNS[1:])
    r = get(srv, "/rr101/api/report/firmware.csv")
    assert r.status == 200 and r.headers["Content-Disposition"] == \
        'attachment; filename="F4-RRORD-101-firmware.csv"'
    assert next(csv.reader(io.StringIO(r.body.decode()))) == list(FIRMWARE_COLUMNS[1:])
    r = get(srv, "/rr101/api/report/inventory.csv")
    assert r.status == 200 and r.headers["Content-Disposition"].endswith('"F4-RRORD-101-inventory.csv"') and r.body.decode().startswith("pos,kind,model,serial,slot")


def test_done_cli(tmp_path, profiles, monkeypatch, capsys):
    from flax_rack import __main__ as cli
    from tests.test_flax_rack_deploy import _cli_dir
    _cli_dir(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="--confirm <order id>"):
        cli.main(["done"])
    with pytest.raises(SystemExit, match="not a host-bearing position: JBOD01"):
        cli.main(["done", "--pos", "JBOD01"])
    assert cli.main(["done", "--confirm", "RRORD-101"]) == 0
    out = capsys.readouterr().out
    assert "C37      end-summary  pass" in out and "F4-RRORD-101-devices.csv" in out
    assert out.rstrip().endswith("done: clean")


def test_drain_file_refuses_new_stages_and_close(srv, tmp_path, monkeypatch):
    from flax_rack import server
    drain = tmp_path / "drain"
    monkeypatch.setattr(server, "DRAIN_FILE", drain)
    drain.touch()
    r = post(srv, "/rr101/api/stage/done", {"positions": ["C37"], "confirm": True})
    assert r.status == 409 and b"being updated" in r.body
    r = post(srv, "/rr101/api/close", {"confirm": "RRORD-101"})
    assert r.status == 409 and b"being updated" in r.body
    drain.unlink()
    assert post(srv, "/rr101/api/stage/done", {"positions": ["C37"], "confirm": True}).status == 202
    _wait_idle(srv)


def test_no_firmware_this_pass_is_flagged(ctx):
    from flax_rack.firmware import run_firmware
    run_firmware(ctx, positions=["C37"])
    done.run(ctx)
    ev = next(e for e in events(ctx) if "no Firmware result" in e)
    assert ev.startswith("done: no Firmware result this pass: L37, ") and "C37" not in ev
    assert ["firmware", done.NO_FIRMWARE] in det(ctx, "L37", "components")["rows"]
    assert all(r[0] != "firmware" for r in det(ctx, "C37", "components")["rows"])
