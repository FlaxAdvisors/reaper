"""HA rows from probe facts (spec §7 infra, §8.3 dormant standby)."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.lib.ha import ha_rows  # noqa: E402
from reapercli.lib.inventory import site_hosts  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
EHV = site_hosts(SITES["eindhoven"], ROOT, "orbit")
BTR = site_hosts(SITES["braintree"], ROOT, "orbit")


def primary(vip=True, standby_ip="192.168.88.22", state="streaming", slot=0, alembic="042"):
    return {"vip": vip, "keepalived": {"state": "active"},
            "pg": {"in_recovery": False, "alembic": alembic, "slot_retained_bytes": slot,
                   "replication": [{"addr": standby_ip, "state": state, "lag_bytes": 0}] if state else []}}


def standby(vip=False, receiver="streaming", lag=0, alembic="042"):
    return {"vip": vip, "keepalived": {"state": "active"},
            "pg": {"in_recovery": True, "alembic": alembic, "wal_receiver": receiver,
                   "replay_lag_bytes": lag}}


def by_item(rows):
    return {(r.item, r.host): r for r in rows}


def statuses(rows):
    return {r.status for r in rows}


def test_healthy_pair_all_ok():
    rows = ha_rows(EHV, {"bang-gouda": primary(), "bang-edam": standby()})
    assert statuses(rows) == {"ok"}
    assert {r.item for r in rows} >= {"vip", "keepalived", "pg-role", "replication", "lag", "alembic", "slot-wal"}


def test_two_primaries_is_split():
    edam = primary(vip=False, state=None)
    rows = ha_rows(EHV, {"bang-gouda": primary(state=None), "bang-edam": edam})
    r = by_item(rows)[("pg-role", "bang-edam")]
    assert r.status == "FAIL" and "split" in r.value


def test_vip_on_standby_warns():
    rows = ha_rows(EHV, {"bang-gouda": primary(vip=False), "bang-edam": standby(vip=True)})
    assert by_item(rows)[("vip", "")].status == "warn"


def test_no_vip_fails():
    rows = ha_rows(EHV, {"bang-gouda": primary(vip=False), "bang-edam": standby()})
    assert by_item(rows)[("vip", "")].status == "FAIL"


def test_pg_error_is_fail_with_reason():
    bad = standby()
    bad["pg"] = {"error": "RuntimeError: No such container: flax-stack-postgres-1"}
    rows = ha_rows(EHV, {"bang-gouda": primary(), "bang-edam": bad})
    r = by_item(rows)[("pg-role", "bang-edam")]
    assert r.status == "FAIL" and "No such container" in r.value


def test_unreachable_bang_is_unreachable_not_fail():
    rows = ha_rows(EHV, {"bang-gouda": primary(), "bang-edam": {"error": "ssh timeout", "unreachable": True}})
    assert by_item(rows)[("keepalived", "bang-edam")].status == "unreachable"
    assert by_item(rows)[("vip", "")].status == "ok"                # gouda answered: it holds it


def test_no_bang_answers_vip_is_unknown_not_missing():
    # fiesta 2026-11-10: neither EHV bang resolved, ha status said "no VIP holder"
    rows = ha_rows(EHV, {"bang-gouda": {"error": "ssh timeout", "unreachable": True}, "bang-edam": {"error": "ssh timeout", "unreachable": True}})
    vip = by_item(rows)[("vip", "")]
    assert vip.status == "unreachable" and "bang-gouda" in vip.value and "bang-edam" in vip.value
    assert "FAIL" not in statuses(rows)


def test_a_probe_that_ran_and_failed_is_still_fail():
    rows = ha_rows(EHV, {"bang-gouda": primary(), "bang-edam": {"error": "probe rc=1: NameError: x"}})
    assert by_item(rows)[("keepalived", "bang-edam")].status == "FAIL"


def test_alembic_mismatch_fails():
    rows = ha_rows(EHV, {"bang-gouda": primary(alembic="042"), "bang-edam": standby(alembic="041")})
    assert by_item(rows)[("alembic", "")].status == "FAIL"


def test_slot_wal_thresholds():
    rows = ha_rows(EHV, {"bang-gouda": primary(slot=2 * 2**30), "bang-edam": standby()})
    assert by_item(rows)[("slot-wal", "bang-gouda")].status == "warn"
    rows = ha_rows(EHV, {"bang-gouda": primary(slot=6 * 2**30), "bang-edam": standby()})
    assert by_item(rows)[("slot-wal", "bang-gouda")].status == "FAIL"


def test_dormant_standby_is_one_warn_no_fail():
    rows = ha_rows(BTR, {"bang-fiesta": primary(state=None), "bang-siesta": {"dormant": True}})
    assert "FAIL" not in statuses(rows)
    warns = [r for r in rows if r.status == "warn"]
    assert len(warns) == 1 and "standby dormant" in warns[0].value
    assert by_item(rows)[("keepalived", "bang-siesta")].status == "dormant"


def test_no_primary_fails():
    rows = ha_rows(EHV, {"bang-gouda": standby(vip=True), "bang-edam": standby()})
    r = [x for x in rows if x.item == "pg-role" and not x.host]
    assert r and r[0].status == "FAIL" and "no primary" in r[0].value


def test_swapped_roles_after_failover_replicate_ok():
    # edam was promoted, gouda re-seeded as its standby: streaming is healthy
    edam = primary(vip=True, standby_ip="192.168.88.21")
    rows = ha_rows(EHV, {"bang-gouda": standby(), "bang-edam": edam})
    assert by_item(rows)[("replication", "")].status == "ok"
    assert by_item(rows)[("vip", "")].status == "warn"           # still not the declared primary


def test_standby_without_facts_keeps_slot_and_fails_replication():
    rows = ha_rows(EHV, {"bang-gouda": primary(state=None, slot=6 * 2**30), "bang-edam": {"error": "ssh timeout", "unreachable": True}})
    assert by_item(rows)[("slot-wal", "bang-gouda")].status == "FAIL"
    rep = by_item(rows)[("replication", "")]
    assert rep.status == "unreachable" and "bang-edam" in rep.value
