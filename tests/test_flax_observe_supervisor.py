"""Unit tests for the flax-observe enrollment supervisor reconcile step.

reconcile_workers is the pure-ish delta engine shared by the initial build and
the supervisor loop. These tests exercise the add/remove/keep-static logic with
a fake worker factory recording start/stop/join -- NO real PortWorker threads,
NO network, NO DB.
"""
from flax_observe.__main__ import reconcile_workers


class FakeWorker:
    def __init__(self, entry):
        self.entry = entry
        self.started = False
        self.stopped = False
        self.joined = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True


def _entry(sw, port, ou=""):
    return {"switch": sw, "port": port, "ou": ou}


def _make_factory():
    made = []

    def make(entry):
        w = FakeWorker(entry)
        made.append(w)
        return w

    return make, made


def test_new_dynamic_key_is_added_and_started():
    key = ("rabbit-lorax", "et5b1")
    desired = {key: _entry(*key)}
    running = {}
    statuses = {}
    make, made = _make_factory()

    reconcile_workers(desired, running, static_keys=set(), make=make,
                      statuses=statuses)

    assert key in running
    assert len(made) == 1
    assert made[0].started is True
    assert running[key] is made[0]


def test_departed_dynamic_key_is_stopped_joined_and_dropped():
    key = ("rabbit-lorax", "et5b1")
    w = FakeWorker(_entry(*key))
    running = {key: w}
    statuses = {key: object()}
    make, made = _make_factory()

    reconcile_workers(desired={}, running=running, static_keys=set(),
                      make=make, statuses=statuses)

    assert key not in running
    assert key not in statuses
    assert w.stopped is True
    assert w.joined is True
    assert made == []  # nothing built


def test_departed_dynamic_key_removes_its_vendor_export_row():
    """Task 4b fix round 1: reconcile_workers' removal branch is the one
    hook that distinguishes "port left the desired set" from "process
    shutdown" (main()'s shutdown loop stops workers directly, never through
    reconcile_workers) -- so this is where the departed port's
    /etc/flax/bmc_vendor.json row must be dropped."""
    import types
    key = ("rabbit-lorax", "et5b1")
    w = FakeWorker(_entry(*key))
    removed = []
    w.env = types.SimpleNamespace(
        vendor_export=types.SimpleNamespace(remove=lambda sw, p: removed.append((sw, p))))
    running = {key: w}
    statuses = {key: object()}
    make, made = _make_factory()

    reconcile_workers(desired={}, running=running, static_keys=set(),
                      make=make, statuses=statuses)

    assert removed == [key]


def test_departed_dynamic_key_without_env_vendor_export_does_not_raise():
    """A FakeWorker (or a real PortWorker whose env has no vendor_export
    attribute) must still be removed cleanly -- the export wiring is
    optional."""
    key = ("rabbit-lorax", "et5b1")
    w = FakeWorker(_entry(*key))  # no .env attribute at all
    running = {key: w}
    statuses = {key: object()}
    make, made = _make_factory()

    reconcile_workers(desired={}, running=running, static_keys=set(),
                      make=make, statuses=statuses)

    assert key not in running
    assert w.stopped is True


def test_static_key_absent_from_desired_is_never_stopped():
    key = ("rabbit-lorax", "et1b1")  # a Triage/turtle static port
    w = FakeWorker(_entry(*key, ou="ou=triage"))
    running = {key: w}
    statuses = {key: object()}
    make, made = _make_factory()

    # Hypothetical: static port dropped out of desired (e.g. fell out of
    # switch_facts access). The static guard must keep its worker alive.
    reconcile_workers(desired={}, running=running, static_keys={key},
                      make=make, statuses=statuses)

    assert key in running
    assert key in statuses
    assert w.stopped is False
    assert w.joined is False


def test_noop_when_desired_equals_running():
    key = ("rabbit-lorax", "et5b1")
    w = FakeWorker(_entry(*key))
    running = {key: w}
    statuses = {key: object()}
    make, made = _make_factory()

    reconcile_workers(desired={key: _entry(*key)}, running=running,
                      static_keys=set(), make=make, statuses=statuses)

    assert running == {key: w}
    assert made == []          # nothing built
    assert w.stopped is False  # nothing torn down
    assert key in statuses


def test_make_worker_forwards_prior_to_portworker(monkeypatch):
    """make_worker must pass its prior row through to PortWorker so a restart
    hydrates."""
    from flax_observe import __main__ as m

    seen = {}

    class FakePortWorker:
        def __init__(self, switch, port, ou, cache, env, *,
                     cycle_secs=10.0, prior=None):
            seen["prior"] = prior
            self._cycle_once = lambda: None

    monkeypatch.setattr(m, "PortWorker", FakePortWorker)

    prior_row = {"vars": {"linkstate": {"value": "link", "since": "x"}},
                 "resolved": {"nic_mac": "52:54:00:11:22:33"}}
    statuses = {}
    w = m.make_worker(
        "rabbit-lorax", "et6b1", "20",
        cache=object(), env=object(), statuses=statuses,
        refresh_sentinels=lambda: None, cycle_secs=10.0, prior=prior_row)
    assert seen["prior"] is prior_row


# --- re-created workers hydrate the CURRENT row (2026-11-06) ----------------
def test_prior_for_uses_boot_snapshot_once_then_reads_current():
    from flax_observe.__main__ import prior_for
    boot = {("sw", "et7b1"): {"vars": {"x": 1}, "resolved": {}}}
    reads = []

    def one(k):
        reads.append(k)
        return {"vars": {"x": 2}, "resolved": {}}
    assert prior_for(("sw", "et7b1"), boot, one)["vars"] == {"x": 1}
    assert prior_for(("sw", "et7b1"), boot, one)["vars"] == {"x": 2}
    assert reads == [("sw", "et7b1")]


def test_prior_for_read_failure_starts_cold():
    from flax_observe.__main__ import prior_for

    def boom(k):
        raise RuntimeError("db")
    assert prior_for(("sw", "p"), {}, boom) is None
