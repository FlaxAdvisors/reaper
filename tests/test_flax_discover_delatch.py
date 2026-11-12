"""The family latch must break on a known family mismatch -- at once, and only then.

WHY (measured live at eindhoven, 2026-09-23). `devices.latched.family` is keyed
by MAC, and on these blades the BMC's MAC is allocated out of the MEZZANINE
CARD's own MAC block -- so the MAC identifies the CARD, not the blade. A card
harvested from a tiogapass and seated in a leopard makes the leopard inherit
`family=tiogapass`; classify turns that into vid 17 instead of 16, reconcile
steers the port onto the tiogapass VLAN, and Kea hands the blade a 172.17.x
address under a `tiogapass...` hostname. et9b2 held ONE devices row carrying
both `family tiogapass` and `product_name Leopard ORv2-DDR4`.

MOVING A BLADE IS NOT A TRIGGER (operator decision, 2026-09-23). The latch
deliberately SURVIVES relocation: that is what keeps a blade identifiable while
it is being moved and its BMC has not come back on a stable address. Only a
sustained, positive contradiction -- the blade's own product_name resolving to a
DIFFERENT known family -- breaks it, on the first cycle that sees it (operator
ruling 2026-09-23: no debounce). Absence is likewise not a trigger:
`run_one_cycle` treats an empty poll as no-data and never wipes a port on it.

ONE MAC CAN APPEAR AT TWO PORTS. When a blade leaves port A, observe HOLDS its
identity at last-known until the link-down is committed (DOWN_DEBOUNCE_SECS=30;
`state_machine.py`: "sub-threshold flap -- HOLD chassissn + inventory at
last-known"). So for ~30s port A still publishes the old blade's `bmc_mac` and
cached `product_name` while port B publishes the same MAC with the new blade's.
`devices` is ON CONFLICT (mac), so both rows write the SAME row -- and if they
disagree about family the value alternates every cycle, which flips
desired_vid and makes reconcile re-steer two live ports, in a loop that
self-feeds through the generation-bump NOTIFY. The cycle must therefore pick ONE
sighting per MAC, and it must pick it from PHYSICAL REALITY, not a heuristic:
the port whose switch MAC address table (switch_facts, published by
flax-switch-sense) currently holds that MAC. A switch never has one MAC on two
ports; only the lease/held-identity view does (operator ruling 2026-09-23).
When the table does not settle it, neither sighting is written that cycle.
"""
import flax_discover.cycle as cyc
from flax_discover.cycle import Discoverer



BMC = "98:03:9b:30:2f:d8"
NIC = "98:03:9b:30:2f:d6"


class FakeDB:
    """A devices table that actually changes when written.

    The earlier fixture returned a FIXED device list, so every cycle re-read the
    same starting state no matter what had been upserted. That models a table
    that cannot change, and it is precisely what hid the two-ports-one-MAC
    defect: a multi-cycle test could not observe a value alternating.
    """

    def __init__(self, observe, facts, devices):
        self.observe, self.facts = observe, facts
        self.devices = {d["mac"]: dict(d) for d in devices}
        self.writes = []

    def read_observe_rows(self, pool):
        return [dict(r) for r in self.observe]

    def read_switch_facts(self, pool):
        return self.facts

    def read_devices(self, pool):
        return [dict(d) for d in self.devices.values()]

    def upsert_device(self, pool, **kw):
        self.writes.append(kw)
        self.devices[kw["mac"]] = {"mac": kw["mac"], "switch": kw["switch"],
                                   "port": kw["port"], "kind": kw["kind"],
                                   "latched": kw["latched"]}

    def families(self):
        return {m: (d["latched"] or {}).get("family")
                for m, d in self.devices.items()}


def _install(monkeypatch, db):
    monkeypatch.setattr(cyc, "read_observe_rows", db.read_observe_rows)
    monkeypatch.setattr(cyc, "read_switch_facts", db.read_switch_facts)
    monkeypatch.setattr(cyc, "read_devices", db.read_devices)
    monkeypatch.setattr(cyc, "upsert_device", db.upsert_device)
    # Not exercised by these fixtures, but one added row would send them into
    # psycopg with pool=None. No-op them so a future fixture cannot trip it.
    if hasattr(cyc, "supersede_port"):
        monkeypatch.setattr(cyc, "supersede_port", lambda pool, **kw: 0)
    if hasattr(cyc, "sweep_vacant_port"):
        monkeypatch.setattr(cyc, "sweep_vacant_port", lambda pool, **kw: 0)


def _disco(tmp_path, **kw):
    (tmp_path / "leopard.txt").write_text("Leopard-DDR4\n")
    (tmp_path / "tiogapass.txt").write_text("Tioga Pass\n")
    return Discoverer(family_map_dir=str(tmp_path), base_secs=30,
                      max_secs=3600, **kw)


def _obs(port, product_name):
    return {"switch": "rabbit-x", "port": port,
            "resolved": {"bmc_mac": BMC, "nic_mac": NIC,
                         "chassis_sn": "QCFF06191403FE",
                         "product_name": product_name}}


def _facts(fdb=None, **links):
    """_facts(Ethernet9_2="link", fdb={"Ethernet9_2": [BMC]}) -> switch_facts
    shape (underscore -> slash). `fdb` is the switch MAC address table per
    port, i.e. switch_facts.ports[<port>].macs."""
    fdb = {k.replace("_", "/"): v for k, v in (fdb or {}).items()}
    names = set(links) | {k.replace("/", "_") for k in fdb}
    ports = {}
    for name in names:
        arista = name.replace("_", "/")
        ports[arista] = {"junk_macs": [], "link": links.get(name, "unknown"),
                         "macs": list(fdb.get(arista, []))}
    return {"rabbit-x": {"ports": ports}}


def _devs(port, family):
    return [{"mac": BMC, "switch": "rabbit-x", "port": port, "kind": "bmc",
             "latched": {"family": family}},
            {"mac": NIC, "switch": "rabbit-x", "port": port, "kind": "host",
             "latched": {"family": family}}]




# --- the latch must SURVIVE a move -------------------------------------------

def test_the_latch_survives_a_move(tmp_path, monkeypatch):
    # Operator decision 2026-09-23: relocation is NOT a trigger. Keeping the
    # family across a move is what lets a blade stay identifiable while it is
    # being moved and its BMC is not yet reachable on a stable address.
    db = FakeDB([_obs("et9b2", None)], _facts(Ethernet9_2="link"),
                _devs("et25b1", "tiogapass"))
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.families()[BMC] == "tiogapass"


def test_a_move_plus_a_mismatch_still_corrects(tmp_path, monkeypatch):
    # A move does not break the latch, but neither does it PROTECT a wrong one:
    # the new blade's own product_name still wins.
    db = FakeDB([_obs("et9b2", "Leopard-DDR4")], _facts(Ethernet9_2="link"),
                _devs("et25b1", "tiogapass"))
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.families()[BMC] == "leopard"


# --- the mismatch trigger: at once, no debounce ------------------------------

def test_a_known_mismatch_breaks_the_latch_on_first_sight(tmp_path, monkeypatch):
    # Operator ruling 2026-09-23: the blade's own product_name resolving to a
    # DIFFERENT KNOWN family breaks the latch as soon as it is seen -- not after
    # a hold-off. One cycle, and both the bmc row and its paired host row move.
    db = FakeDB([_obs("et9b2", "Leopard-DDR4")], _facts(Ethernet9_2="link"),
                _devs("et9b2", "tiogapass"))
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.families()[BMC] == "leopard"
    assert db.families()[NIC] == "leopard"


# --- things that must NOT break the latch ------------------------------------

def test_a_settled_blade_keeps_its_latch(tmp_path, monkeypatch):
    db = FakeDB([_obs("et9b2", "Leopard-DDR4")], _facts(Ethernet9_2="link"),
                _devs("et9b2", "leopard"))
    _install(monkeypatch, db)
    disco = _disco(tmp_path)
    for i in range(3):
        disco.run_one_cycle(pool=None, now=i * 30.0)
    assert db.families()[BMC] == "leopard"


def test_an_unmapped_product_name_does_not_break_a_latch(tmp_path, monkeypatch):
    # product_name is empty or unmapped while a BMC is still coming up. That is
    # not evidence the latch is wrong -- only a DIFFERENT KNOWN family is.
    db = FakeDB([_obs("et9b2", "Some-Unmapped-Board")],
                _facts(Ethernet9_2="link"), _devs("et9b2", "leopard"))
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.families()[BMC] == "leopard"


def test_an_agreeing_reading_never_enters_the_derive_path(tmp_path, monkeypatch):
    # Guards the condition that makes a mismatch a MISMATCH. Dropping the
    # `observed != current` half is invisible in the written family -- breaking
    # the latch and re-deriving lands on the same value -- but it drags every
    # settled blade through the derive path every cycle, churning back-off state
    # and writing _last_pn for known-family MACs. _last_pn is the observable.
    db = FakeDB([_obs("et9b2", "Leopard-DDR4")], _facts(Ethernet9_2="link"),
                _devs("et9b2", "leopard"))
    _install(monkeypatch, db)
    disco = _disco(tmp_path)
    for i in range(3):
        disco.run_one_cycle(pool=None, now=i * 30.0)
    assert disco._last_pn == {}, disco._last_pn
    assert db.families()[BMC] == "leopard"


# --- one MAC at two ports: the switch MAC table decides ----------------------
#
# The two sightings are chosen so that link state AND lexical port order both
# favour the WRONG (held) port: et9b9 sorts after et25b1, and both are given link
# (conservative: a real held row has link down). Only a rule that reads the
# switch MAC address table can pass.

def _two_sightings(fdb):
    return FakeDB([_obs("et9b9", "Tioga Pass"), _obs("et25b1", "Leopard-DDR4")],
                  _facts(fdb=fdb, Ethernet9_9="link", Ethernet25_1="link"),
                  _devs("et9b9", "tiogapass"))


def test_the_switch_mac_table_picks_the_sighting(tmp_path, monkeypatch):
    db = _two_sightings({"Ethernet25_1": [BMC, NIC]})
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.devices[BMC]["port"] == "et25b1"
    assert db.families()[BMC] == "leopard"


def test_the_mac_table_lookup_normalises_case(tmp_path, monkeypatch):
    db = _two_sightings({"Ethernet25_1": [BMC.upper()]})
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.devices[BMC]["port"] == "et25b1"


def test_the_result_does_not_depend_on_row_order(tmp_path, monkeypatch):
    # read_observe_rows has no ORDER BY.
    db = _two_sightings({"Ethernet25_1": [BMC]})
    db.observe.reverse()
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.devices[BMC]["port"] == "et25b1"
    assert db.families()[BMC] == "leopard"


def test_the_family_does_not_alternate_across_cycles(tmp_path, monkeypatch):
    # The flap guard, and why this matters: an alternating family flips
    # desired_vid, which makes reconcile re-steer two live ports, which
    # self-feeds through the generation-bump NOTIFY.
    db = _two_sightings({"Ethernet25_1": [BMC]})
    _install(monkeypatch, db)
    disco = _disco(tmp_path)
    seen = []
    for i in range(4):
        disco.run_one_cycle(pool=None, now=i * 1.5)
        seen.append((db.families()[BMC], db.devices[BMC]["port"]))
    assert seen == [("leopard", "et25b1")] * 4, seen


def test_an_unsettled_duplicate_is_not_written(tmp_path, monkeypatch):
    # Neither port's MAC table holds the MAC (aged out, poll gap): physical
    # reality cannot say which sighting is real, so neither is written -- the
    # same no-data treatment as an empty poll. The row stays as it was.
    db = _two_sightings({})
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert [w for w in db.writes if w["mac"] in (BMC, NIC)] == []
    assert db.devices[BMC]["port"] == "et9b9"
    assert db.families()[BMC] == "tiogapass"


def test_a_mac_both_tables_claim_is_not_written(tmp_path, monkeypatch):
    # Should not happen on a switch; if the facts ever say it, guessing would
    # reintroduce the flap, so it is treated like the unsettled case.
    db = _two_sightings({"Ethernet9_9": [BMC], "Ethernet25_1": [BMC]})
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert [w for w in db.writes if w["mac"] in (BMC, NIC)] == []


def test_a_single_sighting_needs_no_mac_table_entry(tmp_path, monkeypatch):
    # The table is consulted ONLY to settle a duplicate. A MAC seen at one port
    # is written as before even if the table has aged it out -- otherwise a
    # quiet BMC would stop refreshing.
    db = FakeDB([_obs("et9b2", "Leopard-DDR4")], _facts(Ethernet9_2="link"),
                _devs("et9b2", "leopard"))
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert {w["mac"] for w in db.writes} == {BMC, NIC}


# --- only a LIVE reading may break the latch ----------------------------------
#
# Observe HOLDS a departed blade's identity (bmc_mac + cached product_name) while
# its port's link-down is uncommitted (30s), and indefinitely while its switch is
# unreachable. Such a held row is not evidence about the blade. With no
# debounce, letting it break the latch flaps the family (review of d1d50b7,
# reproduced): a live reading is one whose port has link, or whose switch MAC
# table holds the anchor.

def test_a_held_reading_does_not_break_the_latch(tmp_path, monkeypatch):
    # Lone sighting at a link-down port whose MAC table no longer holds it.
    db = FakeDB([_obs("et9b9", "Tioga Pass")], _facts(Ethernet9_9="nolink"),
                _devs("et9b9", "leopard"))
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.families()[BMC] == "leopard"


def test_a_reading_from_an_unreachable_switch_does_not_break_the_latch(
        tmp_path, monkeypatch):
    # The switch is absent from switch_facts: observe holds every port there
    # with no time limit, so this reading may be arbitrarily stale.
    db = FakeDB([_obs("et9b9", "Tioga Pass")], {}, _devs("et9b9", "leopard"))
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.families()[BMC] == "leopard"


def test_a_mac_table_hit_is_live_even_without_link_up(tmp_path, monkeypatch):
    # Either signal is enough: the switch has learned the MAC on that port.
    db = FakeDB([_obs("et9b2", "Leopard-DDR4")],
                _facts(fdb={"Ethernet9_2": [BMC]}, Ethernet9_2="unknown"),
                _devs("et9b2", "tiogapass"))
    _install(monkeypatch, db)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert db.families()[BMC] == "leopard"


def test_a_steer_gap_does_not_flap_the_family(tmp_path, monkeypatch):
    # The reproduced flap. Card moved et9b9 -> et25b1; et9b9 is still held with
    # the OLD blade's reading. Cycle 1 settles the duplicate to et25b1 (leopard).
    # Reconcile then steers et25b1, which flushes its MAC table, so observe at
    # et25b1 briefly sees nothing -- leaving the held et9b9 row as a LONE
    # sighting. It must not break the latch back to tiogapass.
    held_a = _obs("et9b9", "Tioga Pass")
    live_b = _obs("et25b1", "Leopard-DDR4")
    empty_b = {"switch": "rabbit-x", "port": "et25b1",
               "resolved": {"bmc_mac": None, "nic_mac": None}}
    db = FakeDB([held_a, live_b],
                _facts(fdb={"Ethernet25_1": [BMC, NIC]},
                       Ethernet9_9="nolink", Ethernet25_1="link"),
                _devs("et9b9", "tiogapass"))
    _install(monkeypatch, db)
    disco = _disco(tmp_path)
    seen = []
    disco.run_one_cycle(pool=None, now=0.0)
    seen.append((db.families()[BMC], db.devices[BMC]["port"]))
    db.observe = [held_a, empty_b]                                 # steer gap
    db.facts = _facts(Ethernet9_9="nolink", Ethernet25_1="link")
    disco.run_one_cycle(pool=None, now=1.5)
    seen.append(db.families()[BMC])
    db.observe = [held_a, live_b]                                  # re-learned
    db.facts = _facts(fdb={"Ethernet25_1": [BMC]},
                      Ethernet9_9="nolink", Ethernet25_1="link")
    disco.run_one_cycle(pool=None, now=3.0)
    seen.append((db.families()[BMC], db.devices[BMC]["port"]))
    assert seen == [("leopard", "et25b1"), "leopard", ("leopard", "et25b1")], seen
