"""Unit tests for the per-switch fetcher loop."""
import json
import tempfile
from pathlib import Path


def test_load_switches_returns_list_of_dicts():
    from flax_switch_sense.fetcher import load_switches
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "switches.json"
        path.write_text(json.dumps([
            {"name": "rabbit-fiesta", "driver": "eos", "host": "rabbit-fiesta"},
        ]))
        out = load_switches(str(path))
    assert len(out) == 1
    assert out[0]["name"] == "rabbit-fiesta"
    assert out[0]["driver"] == "eos"


def test_load_switches_rejects_non_list():
    import pytest
    from flax_switch_sense.fetcher import load_switches, ConfigError
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.json"
        path.write_text(json.dumps({"not": "a list"}))
        with pytest.raises(ConfigError):
            load_switches(str(path))


def test_make_driver_eos():
    from flax_switch_sense.fetcher import make_driver
    from flax_switch_sense.driver_eos import EosDriver
    entry = {"name": "rabbit-fiesta", "driver": "eos", "host": "rabbit-fiesta"}
    # Mirrors /etc/flax/credentials.json's flat shape (eosuser/eospass)
    creds = {"eosuser": "admin", "eospass": "secret"}
    d = make_driver(entry, creds)
    assert isinstance(d, EosDriver)
    assert d.host == "rabbit-fiesta"
    # eAPI read timeout must tolerate a transient control-plane CPU spike (the
    # hourly EOS `schedule tech-support` job on weak whitebox switches like the
    # WEDGE100S). A 5s timeout trips reachable=false during the spike, which
    # evicts the whole switch from observe's cache. >=15s rides it out.
    assert d._timeout >= 15.0


def test_make_driver_cumulus():
    from flax_switch_sense.fetcher import make_driver
    from flax_switch_sense.driver_cumulus import CumulusDriver
    entry = {"name": "turtle-lorax", "driver": "cumulus", "host": "turtle-lorax"}
    # Mirrors /etc/flax/credentials.json's flat shape (cumuser/cumpass)
    creds = {"cumuser": "root", "cumpass": "test-root-pw"}
    d = make_driver(entry, creds)
    assert isinstance(d, CumulusDriver)
    assert d.host == "turtle-lorax"
    assert d.user == "root"


def test_make_driver_ios():
    from flax_switch_sense.fetcher import make_driver
    from flax_switch_sense.driver_ios import IosDriver
    entry = {"name": "turtle-gouda", "driver": "ios", "host": "turtle-gouda"}
    # Flat cisco_user/cisco_pass, matching flax_reconcile + reaper_leased creds.
    creds = {"cisco_user": "admin", "cisco_pass": "test-root-pw"}
    d = make_driver(entry, creds)
    assert isinstance(d, IosDriver)
    assert d.host == "turtle-gouda"
    assert d.user == "admin"


def test_make_driver_sonic():
    from flax_switch_sense.fetcher import make_driver, driver_name
    from flax_switch_sense.driver_sonic import SonicDriver
    entry = {"name": "rr101-mgmt", "driver": "sonic", "host": "172.26.0.55"}
    d = make_driver(entry, {"sonicuser": "admin", "sonicpass": "test-pw"})
    assert isinstance(d, SonicDriver)
    assert (d.host, d.user, d.password) == ("172.26.0.55", "admin", "test-pw")
    assert driver_name(d) == "sonic"


def test_poll_once_refreshes_driver_each_cycle(monkeypatch):
    """_poll_once must call driver.refresh() once per cycle so the IOS driver
    takes a single batched snapshot (one ssh connection) before the reads."""
    from flax_switch_sense import fetcher as fetcher_mod
    from flax_switch_sense.fetcher import SwitchFetcher

    monkeypatch.setattr(fetcher_mod, "write_switch_facts", lambda row: 1)
    monkeypatch.setattr(fetcher_mod, "write_ack", lambda *a, **k: None)
    monkeypatch.setattr(fetcher_mod, "get_pool", lambda: "POOL")

    class _D:
        def __init__(self):
            self.refreshed = 0

        def refresh(self):
            self.refreshed += 1

        def interfaces_status(self):
            return {}

        def vlans(self):
            return {}

        def mac_address_table(self):
            return []

        def lldp_neighbors_detail(self):
            return {}

    d = _D()
    SwitchFetcher("turtle-gouda", d, cycle_secs=1.0)._poll_once()
    assert d.refreshed == 1


def test_eos_refresh_is_noop():
    """eAPI fetches live per call, so EOS refresh() is a no-op that _poll_once
    can call unconditionally (no network)."""
    from flax_switch_sense.driver_eos import EosDriver
    EosDriver(host="r", user="u", password="p").refresh()  # must not raise/connect


def test_cumulus_refresh_batches_one_ssh():
    """Cumulus refresh() fetches every read in ONE ssh (the batched snapshot),
    so _poll_once gets a single-connection poll -- verify exactly one runner
    call carrying the batch script."""
    from flax_switch_sense.driver_cumulus import CumulusDriver, _BATCH_MARK
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return ""

    CumulusDriver(host="r", user="u", password="p", runner=runner).refresh()
    assert len(calls) == 1
    assert _BATCH_MARK in calls[0]


def test_make_driver_rejects_unknown_driver():
    import pytest
    from flax_switch_sense.fetcher import make_driver, ConfigError
    entry = {"name": "x", "driver": "wat", "host": "x"}
    with pytest.raises(ConfigError):
        make_driver(entry, {})


def test_fetcher_defaults_to_empty_macmath():
    """No macmath supplied -> {} -> every port classifies legacy."""
    from flax_switch_sense.fetcher import SwitchFetcher
    f = SwitchFetcher("rabbit-fiesta", object(), cycle_secs=1.0)
    assert f.macmath_by_vid == {}


def test_fetcher_poll_passes_macmath_by_access_vid(monkeypatch):
    """End-to-end through the fetcher: a port on a macmath vid (26) gets the
    distinct_oui classification in the written switch_facts row; the macmath
    dict reaches classify_macs via the publisher keyed by access_vid."""
    from flax_switch_sense.fetcher import SwitchFetcher
    from flax_switch_sense import publisher

    class _StubDriver:
        def refresh(self):
            pass

        def interfaces_status(self):
            return {"Ethernet23/1": "connected"}

        def vlans(self):
            return {"Ethernet23/1": {"trunk": False, "access_vid": 26}}

        def mac_address_table(self):
            return [
                (26, "3c:2c:99:11:9d:02", "Ethernet23/1"),
                (26, "3c:2c:99:11:9d:00", "Ethernet23/1"),
                (26, "00:90:fb:61:da:a0", "Ethernet23/1"),
            ]

        def lldp_neighbors_detail(self):
            return {}

    captured = {}

    def _fake_write(row):
        captured["row"] = row
        return 7

    monkeypatch.setattr(publisher, "write_switch_facts", _fake_write)
    # fetcher imports the symbol directly, so patch its binding too.
    from flax_switch_sense import fetcher as fetcher_mod
    monkeypatch.setattr(fetcher_mod, "write_switch_facts", _fake_write)

    by_vid = {26: {"scheme": "distinct_oui", "note": "wedge"}}
    f = SwitchFetcher("rabbit-hare", _StubDriver(), cycle_secs=1.0,
                      macmath_by_vid=by_vid)
    f._poll_once()

    p = captured["row"]["ports"]["Ethernet23/1"]
    assert p["bmc_mac"] == "3c:2c:99:11:9d:02"
    assert p["nic_macs"] == ["00:90:fb:61:da:a0"]
    assert p["classification_source"] == "macmath_distinct_oui"


# --- consumer_acks ledger wiring (Task 4) -------------------------------

class _OkDriver:
    """A driver whose poll succeeds with an empty per-port slice."""
    def refresh(self):
        pass

    def interfaces_status(self):
        return {}

    def vlans(self):
        return {}

    def mac_address_table(self):
        return []

    def lldp_neighbors_detail(self):
        return {}


def test_poll_once_acks_applied(monkeypatch):
    """A successful _poll_once writes switch_facts then acks the ledger with
    flax-switch-sense / switches/<switch> / applied at the generation just
    written. The per-switch source keeps each switch's health independent so a
    healthy switch can't clobber a failing one's ack (a shared 'switches' row
    hid partial outages)."""
    from flax_switch_sense import fetcher as fetcher_mod
    from flax_switch_sense.fetcher import SwitchFetcher

    monkeypatch.setattr(fetcher_mod, "write_switch_facts", lambda row: 42)

    acks = []
    monkeypatch.setattr(fetcher_mod, "write_ack",
                        lambda *a, **kw: acks.append((a, kw)))
    monkeypatch.setattr(fetcher_mod, "get_pool", lambda: "POOL")

    f = SwitchFetcher("rabbit-fiesta", _OkDriver(), cycle_secs=1.0)
    f._poll_once()

    assert f.last_generation == 42
    assert len(acks) == 1
    args, kwargs = acks[0]
    assert args == ("POOL", "flax-switch-sense", "switches/rabbit-fiesta",
                    42, "applied")


def test_poll_once_ack_failure_does_not_break_polling(monkeypatch):
    """A ledger write failure must NOT propagate out of _poll_once."""
    from flax_switch_sense import fetcher as fetcher_mod
    from flax_switch_sense.fetcher import SwitchFetcher

    monkeypatch.setattr(fetcher_mod, "write_switch_facts", lambda row: 9)

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(fetcher_mod, "write_ack", _boom)
    monkeypatch.setattr(fetcher_mod, "get_pool", lambda: "POOL")

    f = SwitchFetcher("rabbit-fiesta", _OkDriver(), cycle_secs=1.0)
    f._poll_once()  # must not raise
    assert f.last_generation == 9


def test_run_loop_except_acks_failed(monkeypatch):
    """When _poll_once raises, the run() except path acks failed with the
    error detail truncated to <=200 chars."""
    from flax_switch_sense import fetcher as fetcher_mod
    from flax_switch_sense.fetcher import SwitchFetcher

    # Make the unreachable-row fallback a no-op so we exercise only the ack.
    monkeypatch.setattr(fetcher_mod, "build_switch_facts_row",
                        lambda *a, **kw: {})
    monkeypatch.setattr(fetcher_mod, "write_switch_facts", lambda row: 0)

    acks = []
    monkeypatch.setattr(fetcher_mod, "write_ack",
                        lambda *a, **kw: acks.append((a, kw)))
    monkeypatch.setattr(fetcher_mod, "get_pool", lambda: "POOL")

    f = SwitchFetcher("rabbit-fiesta", _OkDriver(), cycle_secs=0.0)

    boom = RuntimeError("poll exploded")

    def _raise():
        # Stop the loop after this single iteration so run() returns.
        f._stop.set()
        raise boom

    f._poll_once = _raise
    f.run()

    failed = [a for a in acks if a[0][4] == "failed"]
    assert len(failed) == 1
    args, kwargs = failed[0]
    assert args[0] == "POOL"
    assert args[1] == "flax-switch-sense"
    assert args[2] == "switches/rabbit-fiesta"
    assert args[3] == 0  # last_generation None/0 -> 0
    assert args[4] == "failed"
    assert kwargs["detail"].startswith("poll exploded")


def _admin_fetch(monkeypatch, with_method):
    from flax_switch_sense import fetcher as fetcher_mod
    from flax_switch_sense.fetcher import SwitchFetcher
    rows = []
    monkeypatch.setattr(fetcher_mod, "write_switch_facts", lambda row: rows.append(row) or 1)
    monkeypatch.setattr(fetcher_mod, "write_ack", lambda *a, **k: None)
    monkeypatch.setattr(fetcher_mod, "get_pool", lambda: "POOL")

    class _D:
        def refresh(self): pass
        def interfaces_status(self): return {"Ethernet7/1": "nolink", "Ethernet7/2": "nolink"}
        def vlans(self): return {"Ethernet7/1": {"access_vid": 25, "trunk": False},
                                 "Ethernet7/2": {"access_vid": 25, "trunk": False}}
        def mac_address_table(self): return []
        def lldp_neighbors_detail(self): return {}
    if with_method:
        _D.admin_down_ports = lambda self: {"Ethernet7/1"}
    SwitchFetcher("rabbit-edam", _D(), cycle_secs=1.0)._poll_once()
    return rows[0]["ports"]


def test_poll_once_publishes_admin_down_when_driver_offers_it(monkeypatch):
    ports = _admin_fetch(monkeypatch, True)
    assert ports["Ethernet7/1"]["admin"] == "down"
    assert "admin" not in ports["Ethernet7/2"]


def test_poll_once_without_admin_down_ports_method_publishes_no_admin_key(monkeypatch):
    ports = _admin_fetch(monkeypatch, False)
    assert ports["Ethernet7/1"]["mask"] == "access"
    assert all("admin" not in p for p in ports.values())
