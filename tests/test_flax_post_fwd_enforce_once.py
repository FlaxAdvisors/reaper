# tests/test_flax_post_fwd_enforce_once.py
from flax_post.fwd import service


class _Immediate:
    """Executor stand-in: runs submitted fns synchronously so the test is deterministic."""
    def submit(self, fn, *a, **k):
        fn(*a, **k)


class _Deps:
    def __init__(self, ports):
        self._ports = ports
        self.matcher = None; self.fetch = None; self.share_base = "s"
        self.set_row = lambda *a, **k: None
    def post_bmcs(self):
        return [{"port": p, "reservation_ip": "10.0.0." + p[-1]} for p in self._ports]
    def client_for(self, ip):
        return object()


def _reg():
    from flax_post.fwd.service import FlashRegistry
    return FlashRegistry()


def test_detect_mode_submits_nothing():
    ran = []
    service.enforce_once(_Deps(["et6b1"]), _reg(), _Immediate(),
                         mode="detect", allowlist=[],
                         run_node=lambda port, *a, **k: ran.append(port))
    assert ran == []


def test_enforce_submits_only_allowlisted_and_claims():
    ran = []
    reg = _reg()
    service.enforce_once(_Deps(["et6b1", "et7b2"]), reg, _Immediate(),
                         mode="enforce", allowlist=["et6b1"],
                         run_node=lambda port, *a, **k: ran.append(port))
    assert ran == ["et6b1"]                    # allowlist filtered
    assert not reg.is_flashing("et6b1")        # released after run


def test_enforce_skips_port_already_in_flight():
    ran = []
    reg = _reg()
    reg.claim("et6b1")                          # pretend a prior gauntlet holds it
    service.enforce_once(_Deps(["et6b1"]), reg, _Immediate(),
                         mode="enforce", allowlist=[],
                         run_node=lambda port, *a, **k: ran.append(port))
    assert ran == []


class _RecordingDeps(_Deps):
    """_Deps + the phase-4 record hooks (real _Deps grows these in __main__)."""
    def __init__(self, ports):
        super().__init__(ports)
        self.flashes, self.actions = [], []
    def post_bmcs(self):
        return [{"port": p, "reservation_ip": "10.0.0." + p[-1], "mac": "aa:" + p[-1]}
                for p in self._ports]
    def record_flash(self, port, bmc_mac, terminal, timeline):
        self.flashes.append((port, bmc_mac, terminal, timeline))
    def record_action(self, port, bmc_mac, action, ok, detail):
        self.actions.append((port, action, ok))


def _phased_run_node(port, client, matcher, fetch, set_row, share_base, **kw):
    """Fake gauntlet that walks the row through phases, fires one owned action,
    then terminates 'done' — exercises timeline + both hooks."""
    on_action = kw.get("on_action")
    if on_action is not None:
        on_action("power-on", True, "")
    set_row(port, phase="checking")
    set_row(port, phase="flashing", percent=0)
    set_row(port, phase="done", percent=100)
    return "done"


def test_enforce_once_records_flash_with_timeline_and_actions():
    deps = _RecordingDeps(["et6b1"])
    service.enforce_once(deps, _reg(), _Immediate(),
                         mode="enforce", allowlist=[],
                         run_node=_phased_run_node)
    assert len(deps.flashes) == 1
    port, bmc_mac, terminal, timeline = deps.flashes[0]
    assert (port, bmc_mac, terminal) == ("et6b1", "aa:1", "done")
    assert [t["phase"] for t in timeline] == ["checking", "flashing", "done"]
    assert all(isinstance(t["at"], float) for t in timeline)
    assert deps.actions == [("et6b1", "power-on", True)]


def test_enforce_once_without_hooks_still_runs():
    """Plain _Deps (no record_* attrs) must keep working — getattr-guarded."""
    ran = []
    service.enforce_once(_Deps(["et6b1"]), _reg(), _Immediate(),
                         mode="enforce", allowlist=[],
                         run_node=lambda port, *a, **k: ran.append(port) or "done")
    assert ran == ["et6b1"]


def test_detect_mode_records_nothing():
    deps = _RecordingDeps(["et6b1"])
    service.enforce_once(deps, _reg(), _Immediate(),
                         mode="detect", allowlist=[],
                         run_node=_phased_run_node)
    assert deps.flashes == [] and deps.actions == []
