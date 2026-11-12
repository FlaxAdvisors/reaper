"""rackpost Close: archive payload (flax_rack.archive) and close_order's wiring.

Close = archive (flax-control) -> delete the Kea rows -> set_closed -> drop the Deploy
ansible inventory. A failure before set_closed leaves the order open with its
reservations; the payload carries verdicts and artifact paths, never the logs.
"""
import json
import pytest
pytest.importorskip("flax_rack.archive")
from flax_rack import archive, done
from flax_rack.deploy import run_deploy
from flax_rack.kea_client import KeaClient, KeaError
from flax_rack.stages import close_order
from flax_rungs.ansible import INVENTORY
from tests.test_flax_rack_deploy import events, qualified
from tests.test_flax_rack_qualify import profiles  # noqa: F401 -- fixture


@pytest.fixture
def ctx(tmp_path, profiles):
    """Discover + Qualify + Deploy (L37's first boot refused: no installed OS) + Done."""
    c = qualified(tmp_path)
    c.rungs.firstboot_fail = {"L37"}
    run_deploy(c)
    done.run(c)
    return c


def test_payload_no_raw_logs(ctx):
    p = archive.payload(ctx.g, ctx.db, ctx.dir)
    blob = json.dumps(p)
    lr = ctx.db.latest_runs()
    sel = lr[("C37", "end-summary")]["artifacts"]["sel-elist"]
    assert sel in blob                                       # the path is archived ...
    text = (ctx.dir / sel).read_text()
    assert text.strip() and text.strip().splitlines()[0] not in blob   # ... not its text
    for r in p["rungs"]:
        assert all(isinstance(a, str) and a.startswith("artifacts/") for a in r["artifacts"])
    keys = {(r["pos"], r["stage"], r["rung"]) for r in p["rungs"]}
    assert len(keys) == len(p["rungs"]) and ("C37", "done", "end-summary") in keys
    dev = {d["pos"]: d for d in p["devices"]}
    assert dev["C37"]["final_verdict"] == "deployed"
    assert dev["L37"]["final_verdict"].startswith("not deployed: firstboot")
    assert dev["JBOD01"]["final_verdict"] == "n/a"
    assert all(dev[k]["final_verdict"] == "n/a" for k in dev
               if k.startswith("_sw-") or ".iom" in k)
    roles = {(m["pos"], m["role"]) for m in p["macs"]}
    assert {("C37", "host"), ("C37", "bmc"), ("C37", "fabric_a"), ("C37", "fabric_b"),
            ("JBOD01.iom1", "iom1")} <= roles
    mac = {(m["pos"], m["role"]): m["mac"] for m in p["macs"]}
    # never deployed: the PCIe ports Qualify's ha-path saw, not another device's bond
    assert mac[("L37", "fabric_a")] not in (mac[("C37", "fabric_a")], mac[("L37", "fabric_b")])
    assert {c["pos"] for c in p["components"]} >= {"C37", "L37"}
    assert p["order_id"] == ctx.g.order and p["rack"] == ctx.g.rack


def test_close_archives_releases_and_drops_inventory(ctx):
    inv = ctx.dir / INVENTORY
    inv.write_text("[nodes]\n172.32.37.51 ansible_user=flax rackpost_pos=C37\n")
    out = close_order(ctx)
    assert ctx.db.closed_at() and not inv.exists()
    (p,) = ctx.kea.archives
    assert p["closed_at"] == ctx.db.closed_at()
    assert out["archived"]["devices"] == len(p["devices"]) and out["released"]
    assert events(ctx)[0] == (f"closed; archived {len(p['devices'])} device(s); "
                              f"{out['released']} reservation(s) released")


def test_close_components_from_latest_inventory_without_done(tmp_path, profiles):
    c = qualified(tmp_path)                  # Qualify ran, Done never did
    assert not c.db.components()
    close_order(c)
    (p,) = c.kea.archives
    assert {x["pos"] for x in p["components"]} >= {"C37", "L37"}


def test_close_release_fail_withdraws_first_archive(ctx):
    ctx.kea.fail_delete = True
    with pytest.raises(KeaError):
        close_order(ctx)
    assert ctx.db.closed_at() is None
    p = ctx.kea.archives[-1]                 # the last write: no rows left archived
    assert not (p["devices"] or p["macs"] or p["components"] or p["rungs"])
    assert "reservations NOT released" in events(ctx)[0] and "withdrawn" in events(ctx)[0]
    ctx.kea.fail_delete = False
    close_order(ctx)
    p = ctx.kea.archives[-1]
    assert p["devices"] and p["closed_at"] == ctx.db.closed_at()
    ctx.kea.fail_delete = True               # a re-close never withdraws a closed order
    with pytest.raises(KeaError):
        close_order(ctx)
    p = ctx.kea.archives[-1]
    assert p["devices"] and ctx.db.closed_at()


def test_close_inventory_unlink_failure_still_closes(ctx, monkeypatch):
    inv = ctx.dir / INVENTORY
    inv.write_text("[nodes]\n")
    real = type(inv).unlink

    def refuse(self, missing_ok=False):
        if self.name == inv.name:
            raise PermissionError(13, "Permission denied")
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(type(inv), "unlink", refuse)
    out = close_order(ctx)
    assert ctx.db.closed_at() and out["archived"]["devices"]
    assert any(e["kind"] == "error" and "not removed" in e["text"] for e in ctx.db.events())


def test_close_archive_fail_keeps_order_open(ctx):
    inv = ctx.dir / INVENTORY
    inv.write_text("[nodes]\n")
    ctx.kea.fail_archive = True
    held = len(ctx.kea.upserts)
    with pytest.raises(KeaError):
        close_order(ctx)
    assert ctx.db.closed_at() is None
    assert len(ctx.kea.upserts) == held and held       # delete_source never called
    assert inv.exists()
    ctx.kea.fail_archive = False                       # Close is re-runnable
    assert close_order(ctx)["released"] == held and ctx.db.closed_at()


def test_client_archive_posts_to_order_archive():
    seen = []

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"devices": 1, "components": 0, "macs": 0, "rungs": 0}'

    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url, json.loads(req.data)))
        return _R()
    out = KeaClient("http://fc:8000", opener=opener).archive({"order_id": "X"})
    assert out["devices"] == 1
    assert seen == [("POST", "http://fc:8000/api/v1/order-archive", {"order_id": "X"})]


def test_payload_identity_blocks(ctx):
    """Every device's identity read is archived as the drawer renders it; an IOM's
    and a switch's serial come from their own read; a JBOD (a geometry device with
    no inventory) gets no borrowed serial; a switch read carrying nothing is null."""
    from flax_rungs.result import RungResult
    rid = ctx.db.start_run("_sw-C", "discover", "serial")
    ctx.db.finish_run(rid, RungResult("pass", {
        "serial": "TW05WPCHDNT0008R0058", "part_number": "05WPCH",
        "platform": "x86_64-dellemc_n3248te_c3338-r0", "service_tag": "7PN8PK2",
        "rows": [["Serial Number", "TW05WPCHDNT0008R0058"], ["Service Tag", "7PN8PK2"],
                 ["Product Name", "N3248TE-ON"]]},
        {"syseeprom": "artifacts/_sw-C/d1/serial/syseeprom.txt"}))
    devs = {d["pos"]: d for d in archive.payload(ctx.g, ctx.db, ctx.dir)["devices"]}

    sw = devs["_sw-C"]
    assert sw["chassis_serial"] == "TW05WPCHDNT0008R0058"
    rows = sw["identity"]["rows"]
    # the facts the read's rows lack come first; the service tag is not repeated
    assert rows[:2] == [["part number", "05WPCH"],
                        ["platform", "x86_64-dellemc_n3248te_c3338-r0"]]
    assert ["Service Tag", "7PN8PK2"] in rows and sum(r[1] == "7PN8PK2" for r in rows) == 1
    assert sw["identity"]["raw"] == {"syseeprom": "artifacts/_sw-C/d1/serial/syseeprom.txt"}
    assert devs["_sw-B"]["identity"] is None and devs["_sw-B"]["chassis_serial"] is None

    iom = devs["JBOD01.iom1"]
    assert iom["chassis_serial"] == iom["identity"]["serial"] and iom["chassis_serial"]
    assert iom["identity"]["title"] == "Board Serial"
    assert ["IOM Location", "Side A"] in iom["identity"]["rows"]
    assert devs["JBOD01"]["chassis_serial"] is None

    host = devs["C37"]
    assert host["identity"]["title"] == "Product Serial"
    assert [f"serial (Product Serial)", host["chassis_serial"]] in host["identity"]["rows"]
    assert all(p.startswith("artifacts/") for p in host["identity"]["raw"].values())


def test_sales_order_in_payload_and_report(ctx):
    """geometry "sales" rides the Close payload and the process report header; the
    archive key stays the order id. Without it the payload carries null."""
    import dataclasses
    from flax_rack import process_report
    assert archive.payload(ctx.g, ctx.db, ctx.dir)["sales"] is None
    g = dataclasses.replace(ctx.g, sales="S-ORD101031")
    p = archive.payload(g, ctx.db, ctx.dir)
    assert (p["order_id"], p["sales"]) == (ctx.g.order, "S-ORD101031")
    html = process_report.render(process_report.build(ctx.dir, g, ctx.db))
    assert "sales order S-ORD101031" in html
    assert "sales order" not in process_report.render(process_report.build(ctx.dir, ctx.g, ctx.db))


def _unpack(a):
    import base64, gzip
    return gzip.decompress(base64.b64decode(a["gzip_b64"]))


def test_payload_carries_the_files(ctx):
    """040: every file a rung references rides the payload gzip'd, byte-exact, with its
    sha256; the order's CSVs ride as documents; the rung rows still carry the paths."""
    import hashlib
    p = archive.payload(ctx.g, ctx.db, ctx.dir)
    arts = {a["path"]: a for a in p["artifacts"]}
    referenced = {x for r in p["rungs"] for x in r["artifacts"]}
    assert referenced and referenced <= set(arts)
    sel = ctx.db.latest_runs()[("C37", "end-summary")]["artifacts"]["sel-elist"]
    a = arts[sel]
    data = (ctx.dir / sel).read_bytes()
    assert a["status"] == "stored" and _unpack(a) == data
    assert (a["size"], a["sha256"]) == (len(data), hashlib.sha256(data).hexdigest())
    assert a["content_type"] == "text/plain"
    csvs = [n for n in arts if n.endswith(".csv")]
    assert csvs and all("/" not in n and arts[n]["content_type"] == "text/csv" for n in csvs)


def test_payload_files_too_large_missing_and_outside(ctx, monkeypatch):
    p0 = archive.payload(ctx.g, ctx.db, ctx.dir)
    big = max((a for a in p0["artifacts"] if a["status"] == "stored"), key=lambda a: a["size"])
    monkeypatch.setattr(archive, "MAX_FILE", big["size"] - 1)
    gone = next(a["path"] for a in p0["artifacts"] if a["path"] != big["path"]
                and a["path"].startswith("artifacts/"))
    (ctx.dir / gone).unlink()
    arts = {a["path"]: a for a in archive.payload(ctx.g, ctx.db, ctx.dir)["artifacts"]}
    assert arts[big["path"]]["status"] == "too_large" and "gzip_b64" not in arts[big["path"]]
    assert arts[gone] == {"path": gone, "status": "missing"}
    (ctx.dir.parent / "secret.txt").write_text("outside")
    rows = archive.artifact_rows(ctx.dir, ["../secret.txt", "/etc/hostname", "a b.txt"])
    assert rows == []                                   # never read, never listed


def test_withdraw_empties_files(ctx):
    sent = []
    ctx.kea.archive = lambda p: sent.append(p) or {}
    from flax_rack.stages import _retract
    _retract(ctx, archive.payload(ctx.g, ctx.db, ctx.dir))
    assert sent[-1]["artifacts"] == [] and sent[-1]["rungs"] == []
