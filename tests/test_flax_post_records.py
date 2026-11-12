"""Pure-logic tests for flax_post/records.py (work-record writer helpers).

The DB-touching functions (resolve_dut/append_record/orchestrators) are
covered by tests/test_flax_post_work_records_db.py (FLAX_TEST_DSN, self-skip).
"""
from flax_post import records


def test_norm_mac_shapes():
    assert records.norm_mac("AA:BB:CC:DD:EE:0F") == "aa:bb:cc:dd:ee:0f"
    assert records.norm_mac("aabbccddee0f") == "aa:bb:cc:dd:ee:0f"
    assert records.norm_mac("aa-bb-cc-dd-ee-0f") == "aa:bb:cc:dd:ee:0f"
    assert records.norm_mac("") is None
    assert records.norm_mac(None) is None
    assert records.norm_mac("not-a-mac") is None


def test_serial_fault_placeholder_and_missing():
    assert records.serial_fault("") is not None
    assert records.serial_fault(None) is not None
    assert records.serial_fault("   ") is not None
    assert records.serial_fault("To Be Defined By Customer") is not None
    assert records.serial_fault("0123456789") is not None
    assert records.serial_fault("WTH491259XYZ0A") is None


def test_canonical_hash_stable_and_key_order_free():
    a = records.canonical_hash({"x": 1, "y": [1, 2]})
    b = records.canonical_hash({"y": [1, 2], "x": 1})
    assert a == b and len(a) == 64
    assert records.canonical_hash({"x": 2, "y": [1, 2]}) != a


def test_sel_delta():
    prev = [{"id": "1", "ts": "07/01/2026 10:00:00", "event": "boot"}]
    cur = prev + [{"id": "2", "ts": "07/02/2026 11:00:00", "event": "dimm ce"}]
    assert records.sel_delta(prev, cur) == [cur[1]]
    assert records.sel_delta(None, prev) == prev      # first sighting: all new
    assert records.sel_delta(prev, prev) == []        # steady state: nothing
    assert records.sel_delta(prev, []) == []          # SEL cleared: not an event


def test_role_keys_omits_nulls():
    assert records.role_keys({"order_no": "RRORD-003", "customer": None}) == \
        {"order": "RRORD-003"}
    assert records.role_keys({"order_no": None, "customer": ""}) == {}
    assert records.role_keys({"order_no": "R", "customer": "C"}) == \
        {"order": "R", "customer": "C"}


# --- inventory carry-forward (2026-09-19): an empty SDR/FRU read is "no answer
# this pass", not "no sensors" -- it must not flip the inventory hash. ---
from flax_post.records import inventory_payload, canonical_hash

_FRU = {"Product Serial": "WTH0000000M1A", "Board Product": "Tioga Pass Single Side"}
_SDR = {"CPU0 Temp": {}, "P1V05": {}, "DIMM A0 Temp": {}}


def test_inventory_payload_full_read_is_what_was_read():
    assert inventory_payload(_FRU, _SDR, None) == {"fru": _FRU, "sdr_sensors": sorted(_SDR)}


def test_inventory_payload_empty_sdr_carries_previous_sensors():
    prev = inventory_payload(_FRU, _SDR, None)
    assert inventory_payload(_FRU, {}, prev) == prev


def test_inventory_payload_empty_fru_carries_previous_fru():
    prev = inventory_payload(_FRU, _SDR, None)
    assert inventory_payload({}, _SDR, prev) == prev


def test_inventory_payload_flip_flop_hashes_stay_equal():
    prev = inventory_payload(_FRU, _SDR, None)
    prev["hash"] = canonical_hash(prev)          # stored records carry their hash
    h_full = canonical_hash(inventory_payload(_FRU, _SDR, prev))
    h_empty = canonical_hash(inventory_payload(_FRU, None, prev))
    assert h_full == h_empty == prev["hash"]


def test_inventory_payload_real_change_still_differs():
    prev = inventory_payload(_FRU, _SDR, None)
    fewer = {k: v for k, v in _SDR.items() if k != "DIMM A0 Temp"}   # a DIMM pulled
    assert inventory_payload(_FRU, fewer, prev)["sdr_sensors"] == sorted(fewer)
    other = dict(_FRU, **{"Product Serial": "WTH1111111M1A"})
    assert inventory_payload(other, {}, prev) == {"fru": other, "sdr_sensors": sorted(_SDR)}


def test_inventory_payload_first_read_empty_has_nothing_to_carry():
    assert inventory_payload({}, {}, None) == {"fru": {}, "sdr_sensors": []}


def test_inventory_payload_ignores_prev_hash_key():
    prev = dict(inventory_payload(_FRU, _SDR, None), hash="x")
    assert "hash" not in inventory_payload(_FRU, {}, prev)
