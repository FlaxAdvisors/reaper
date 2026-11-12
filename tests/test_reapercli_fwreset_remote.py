"""reaper fw: which agent rows get a fresh evaluation (spec 2026-11-11 §7).

decide() is pure; triage's own predicates arrive in `deps` (in the api container they
are biosfw.retry.in_flight, biosfw.hold.Hold().held, bmcfw.store.Claim().active and
bmcfw.store.cleared_record). Here they are simple stand-ins with the same contract."""
import json
import os
import types

import pytest

pytest.importorskip("reapercli")
from reapercli import fwreset_remote as fr  # noqa: E402

NEW = "flax-onetree-1.1.6"
OLD = "flax-onetree-1.1.5"


def deps(held=(), claimed=(), in_flight_phases=("flashing", "verifying")):
    def cleared(rec):          # = bmcfw.store.cleared_record's contract
        if rec.get("phase") != "fault":
            return rec
        if not rec.get("hold_power_on"):
            return None
        return dict(rec, phase="backup_pending", fault_reason="")
    return types.SimpleNamespace(
        in_flight=lambda rec: bool(rec.get("power_lock") or rec.get("phase") in in_flight_phases),
        held=lambda port: port in held,
        claim_active=lambda port: port in claimed,
        cleared_record=cleared)


def row(phase, target=OLD, port="et1b1", **kw):
    return dict(port=port, phase=phase, target_version=target, **kw)


def d(lane, rec, in_family=True, **kw):
    return fr.decide(lane, rec, in_family, NEW, deps(**kw))


def test_not_in_feed_is_skipped():
    assert d("bmc", row("needs_update"), in_family=None) == ("skip", "not in feed")


def test_other_family_and_new_target_kept():
    assert d("bmc", row("needs_update"), in_family=False)[0] == "keep"
    assert d("bmc", row("needs_update", target=NEW))[0] == "keep"


@pytest.mark.parametrize("phase", ["needs_update", "up_to_date", "done", "target_mismatch", "oem",
                                   "unresolved", "unmatched", "fault"])
def test_bmc_resting_rows_deleted(phase):
    assert d("bmc", row(phase))[0] == "delete"


@pytest.mark.parametrize("phase", ["blocked", "done", "up_to_date", "fault"])
def test_bios_resting_rows_deleted(phase):
    assert d("bios", row(phase, target="TPO_P26F"))[0] == "delete"


def test_blank_target_counts_as_old():
    assert d("bmc", row("needs_update", target=""))[0] == "delete"


def test_bios_in_flight_skipped():
    assert d("bios", row("flashing")) == ("skip", "in flight")
    assert d("bios", row("blocked", power_lock=True)) == ("skip", "in flight")


def test_bmc_claimed_skipped():
    assert d("bmc", row("needs_update"), claimed={"et1b1"}) == ("skip", "in flight")


def test_bmc_unknown_phase_is_mid_drive():
    assert d("bmc", row("backup_writing")) == ("skip", "phase backup_writing: mid-drive")


def test_bios_hold_skipped():
    assert d("bios", row("fault"), held={"et1b1"}) == ("skip", "operator hold")
    assert d("bios", row("held")) == ("skip", "operator hold")
    assert d("bios", row("fault", hold_set=True)) == ("skip", "operator hold")


@pytest.mark.parametrize("phase", ["needs_power_cycle", "chip_unrecoverable"])
def test_bios_dark_phases_skipped(phase):
    assert d("bios", row(phase))[0] == "skip"


def test_bios_confirmed_left_to_the_worker():
    assert d("bios", row("confirmed")) == ("keep", "confirmed: bios_fw demotes it on a target move")


def test_bmc_fault_with_power_on_debt_reparked():
    act, why = d("bmc", row("fault", hold_power_on=True))
    assert act == "repark"


def test_bmc_non_fault_with_debt_skipped():
    assert d("bmc", row("up_to_date", hold_power_on=True)) == ("skip", "power-on debt")


def test_repark_record_carries_new_target():
    rec = fr.repark(row("fault", hold_power_on=True), NEW, deps())
    assert rec["phase"] == "backup_pending" and rec["target_version"] == NEW


# ------------------------------------------------------------ main() against a tmp store
class FakeStore:
    """bmcfw/biosfw Store's read + remove/put surface, in place on a tmp file."""
    def __init__(self, path):
        self.path = path

    def all(self):
        with open(self.path) as f:
            return json.load(f)

    def _save(self, data):
        fd = os.open(self.path, os.O_WRONLY)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(data).encode())
        finally:
            os.close(fd)

    def remove(self, port):
        data = self.all()
        data.pop(port, None)
        self._save(data)

    def put(self, port, rec):
        data = self.all()
        data[port] = rec
        self._save(data)


def run_main(tmp_path, rows, feed, apply):
    store = tmp_path / "bmcfw.json"
    store.write_text(json.dumps(rows))
    os.chmod(store, 0o600)
    entry = {"target_version": NEW, "match": {}}
    env = fr.Env(store=FakeStore(str(store)), deps=deps(),
                 family_entry=entry, match=lambda feed_row: entry if feed_row.get("product_name") == "tp" else {},
                 feed=lambda: feed, now="20261111T120000Z")
    spec = {"lane": "bmc", "family": "tiogapass-openbmc", "new_target": NEW, "apply": apply,
            "store": str(store)}
    return store, fr.run(spec, env)


def test_apply_in_place_keeps_inode_and_mode(tmp_path):
    rows = {"et1b1": row("needs_update"), "et1b2": row("flashing", port="et1b2")}
    store, before = tmp_path / "bmcfw.json", None
    store.write_text("{}")
    ino = os.stat(store).st_ino
    store, out = run_main(tmp_path, rows, {"et1b1": {"product_name": "tp"}, "et1b2": {"product_name": "tp"}}, True)
    assert os.stat(store).st_ino == ino and oct(os.stat(store).st_mode & 0o777) == "0o600"
    assert set(json.loads(store.read_text())) == {"et1b2"}
    assert out["applied"] and out["backup"].endswith(".reaperfw-20261111T120000Z")
    assert json.loads(open(out["backup"]).read()) == rows


def test_plan_changes_nothing(tmp_path):
    rows = {"et1b1": row("needs_update")}
    store, out = run_main(tmp_path, rows, {"et1b1": {"product_name": "tp"}}, False)
    assert json.loads(store.read_text()) == rows
    assert out["applied"] is False and out["backup"] is None
    assert out["rows"] == [{"port": "et1b1", "phase": "needs_update", "target": OLD,
                            "action": "delete", "reason": "needs_update on old target"}]


def test_port_missing_from_feed_is_skipped(tmp_path):
    rows = {"et9b9": row("needs_update", port="et9b9")}
    store, out = run_main(tmp_path, rows, {}, True)
    assert json.loads(store.read_text()) == rows
    assert out["rows"][0]["action"] == "skip"


def test_ports_filter_for_the_standby(tmp_path):
    """The standby has no feed: it cleans exactly the ports the holder cleaned."""
    rows = {"et1b1": row("needs_update"), "et1b2": row("needs_update", port="et1b2")}
    store = tmp_path / "bmcfw.json"
    store.write_text(json.dumps(rows))
    env = fr.Env(store=FakeStore(str(store)), deps=deps(), family_entry={"target_version": NEW},
                 match=None, feed=None, now="20261111T120000Z")
    out = fr.run({"lane": "bmc", "family": "f", "new_target": NEW, "apply": True, "store": str(store),
                  "ports": ["et1b1"]}, env)
    assert set(json.loads(store.read_text())) == {"et1b2"}
    assert [r["action"] for r in out["rows"]] == ["delete", "keep"]


def test_no_feed_and_no_ports_touches_nothing(tmp_path):
    rows = {"et1b1": row("needs_update")}
    store = tmp_path / "bmcfw.json"
    store.write_text(json.dumps(rows))
    env = fr.Env(store=FakeStore(str(store)), deps=deps(), family_entry={"target_version": NEW},
                 match=None, feed=None, now="t")
    out = fr.run({"lane": "bmc", "family": "f", "new_target": NEW, "apply": True, "store": str(store)}, env)
    assert json.loads(store.read_text()) == rows and out["rows"][0]["action"] == "skip"


def test_manifest_from_spec_overrides_the_rendered_file(tmp_path):
    """The plan previews rows against the NEW manifest before it is rendered."""
    data = {"tiogapass-openbmc": {"target_version": NEW}}
    rendered = tmp_path / "m.json"
    rendered.write_text(json.dumps({"tiogapass-openbmc": {"target_version": OLD}}))
    assert fr.manifest_data({"manifest": str(rendered), "manifest_json": data}) == data
    assert fr.manifest_data({"manifest": str(rendered)})["tiogapass-openbmc"]["target_version"] == OLD


def test_bmc_lane_respects_bios_operator_hold():
    """review I5: bmc_fw powers a host off to flash; a bios-fw-hold pins it dark."""
    assert d("bmc", row("up_to_date"), held={"et1b1"}) == ("skip", "operator hold")


def test_deadline_passed_writes_nothing(tmp_path):
    """review I4: the ssh side gives up at its timeout; the script must not write after."""
    rows = {"et1b1": row("needs_update")}
    store = tmp_path / "bmcfw.json"
    store.write_text(json.dumps(rows))
    ticks = iter([0.0, 500.0])
    env = fr.Env(store=FakeStore(str(store)), deps=deps(), family_entry={"target_version": NEW},
                 match=lambda r: {"target_version": NEW}, feed=lambda: {"et1b1": {}}, now="t",
                 clock=lambda: next(ticks))
    with pytest.raises(fr.Deadline):
        fr.run({"lane": "bmc", "family": "f", "new_target": NEW, "apply": True, "store": str(store),
                "deadline_s": 200}, env)
    assert json.loads(store.read_text()) == rows and list(tmp_path.glob("*.reaperfw-*")) == []
