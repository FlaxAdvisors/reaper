# tests/test_flax_post_host_qual.py
"""host_qual: poll the node agent, map to vars.qual, capture terminal artifacts once."""
from flax_post.observe import host_qual


class FakeAgent:
    """In-memory fake honoring the frozen REST contract."""
    def __init__(self, run_id="r1", status_body=None, stages=None, arts=None, summaries=None):
        self._run_id = run_id
        self._status = status_body or {}
        self._stages = stages or []
        self._arts = arts or {}   # {(stage, art): text}
        self._summaries = summaries or {}   # {stage: summary dict}
    def health(self): return {"ok": True, "mac": "aa:bb", "serial": "SN1",
                              "run_id": self._run_id, "state": "running", "agent_ver": "1"}
    def status(self): return self._status
    def stages(self): return self._stages
    def stage(self, name):
        return next(s | {"summary": self._summaries.get(name, {}), "artifacts":
                         [{"name": a, "kind": "raw", "bytes": len(t)}
                          for (st, a), t in self._arts.items() if st == name]}
                    for s in self._stages if s["name"] == name)
    def stage_artifact(self, name, art): return self._arts[(name, art)]


def _store():
    class S:
        def __init__(self): self.state = {}; self.arts = {}
        def set_state(self, port, **kw): self.state.setdefault(port, {}).update(kw)
        def write_artifact(self, bmc_mac, run_id, stage, name, kind, content, **kw):
            self.arts[(bmc_mac, run_id, stage, name)] = content
        def list_artifacts(self, bmc_mac, run_id, stage=None):
            return [{"stage": s, "name": n, "kind": "raw", "bytes": len(c)}
                    for (m, r, s, n), c in self.arts.items()
                    if m == bmc_mac and r == run_id and (stage is None or s == stage)]
        def get_artifact(self, bmc_mac, run_id, stage, name):
            return self.arts.get((bmc_mac, run_id, stage, name))
        def read_state(self): return self.state
        def read_settings(self): return {"population": None}
    return S()


TARGET = {"port": "et6b1", "host_ip": "10.0.0.5", "bmc_ip": "10.0.0.6",
          "bmc_mac": "aa:bb", "serial": "SN1", "order_no": "RRORD-003"}


def test_agent_step_pass_when_reachable():
    assert host_qual.agent_step(True) == {"agent-reachable": {"status": "pass"}}


def test_agent_step_pending_when_unreachable():
    assert host_qual.agent_step(False) == {"agent-reachable": {"status": "pending"}}


def test_poll_maps_status_and_steps_into_vars_qual():
    agent = FakeAgent(status_body={"status": "running", "current": "iperf",
                                    "done_n": 3, "total_n": 11, "verdict": None, "pct": 27},
                      stages=[{"name": "sdr-pre", "status": "pass"},
                              {"name": "iperf", "status": "running"}])
    store = _store()
    qual = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert qual["run_id"] == "r1"
    assert qual["agent"]["reachable"] is True
    assert qual["overall"]["current"] == "iperf"
    assert qual["steps"]["sdr-pre"]["status"] == "pass"
    assert store.state["et6b1"]["qual"]["overall"]["pct"] == 27


def test_terminal_stage_artifacts_captured_once():
    agent = FakeAgent(stages=[{"name": "inventory", "status": "pass"}],
                      status_body={"status": "running", "current": "inventory",
                                   "done_n": 1, "total_n": 11, "verdict": None, "pct": 9},
                      arts={("inventory", "dmidecode"): "Handle 0x0001",
                            ("inventory", "macinv"): "12 Memory Size: 64 GB"})
    store = _store()
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert store.arts[("aa:bb", "r1", "inventory", "dmidecode")] == "Handle 0x0001"
    # Second poll: already stored -> not re-fetched (capture-once).
    calls = {"n": 0}
    agent.stage_artifact = lambda n, a: calls.__setitem__("n", calls["n"] + 1) or "x"
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert calls["n"] == 0


def test_unreachable_agent_marks_reachable_false():
    from flax_post.qualclient import QualUnreachable
    class Dead:
        def health(self): raise QualUnreachable("down")
    store = _store()
    qual = host_qual.poll_target(TARGET, make_client=lambda ip: Dead(), store=store)
    assert qual["agent"]["reachable"] is False
    assert store.state["et6b1"]["qual"]["agent"]["reachable"] is False
    # steps must be dict-shaped on the unreachable path too (Task 8 reads steps[name]["status"])
    assert qual["steps"]["agent-reachable"] == {"status": "pending"}
    assert all(isinstance(v, dict) for v in store.state["et6b1"]["qual"]["steps"].values())


def test_run_once_guards_each_target():
    class Boom:
        def health(self): raise RuntimeError("kaboom")
    store = _store()
    # Must not raise even though the only target's client explodes.
    host_qual.run_once([TARGET], make_client=lambda ip: Boom(), store=store)


def test_population_check_green_red_grey(monkeypatch):
    from flax_post.observe import host_qual
    monkeypatch.setattr(host_qual.population, "load_profile",
                        lambda name: ["12 Memory Size: 64 GB", "BIOS Version: TPC_P26C"])
    green = host_qual.population_check("12 Memory Size: 64 GB\nBIOS Version: TPC_P26C", "ovh-bare-tp")
    assert green["verdict"] == "green" and green["failed_rules"] == []
    red = host_qual.population_check("8 Memory Size: 32 GB", "ovh-bare-tp")
    assert red["verdict"] == "red" and "12 Memory Size: 64 GB" in red["failed_rules"]
    grey = host_qual.population_check("", "ovh-bare-tp")
    assert grey["verdict"] == "grey"
    grey2 = host_qual.population_check("anything", None)
    assert grey2["verdict"] == "grey"


def test_poll_writes_pop_and_population_check_step(monkeypatch):
    from flax_post.observe import host_qual
    monkeypatch.setattr(host_qual.population, "load_profile",
                        lambda name: ["12 Memory Size: 64 GB"])
    monkeypatch.setattr(host_qual, "_read_profile_for",
                        lambda target, store: "ovh-bare-tp")
    agent = FakeAgent(stages=[{"name": "inventory", "status": "pass"}],
                      status_body={"status": "running", "current": "inventory",
                                   "done_n": 1, "total_n": 11, "verdict": None, "pct": 9},
                      arts={("inventory", "macinv"): "12 Memory Size: 64 GB"})
    store = _store()
    qual = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert store.state["et6b1"]["pop"]["verdict"] == "green"
    assert qual["steps"]["population-check"]["status"] == "pass"


_NO_SERIAL = {"state": "no_serial", "serial": None, "serial_field": "Product Serial"}


def test_population_no_ship_serial_is_red_with_a_profile(monkeypatch):
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: ["12 Memory Size: 64 GB"])
    out = host_qual.population_check("12 Memory Size: 64 GB", "ovh-bare-tp", baseboard=_NO_SERIAL)
    assert out["verdict"] == "red" and out["no_ship_serial"] is True
    assert out["failed_rules"] == ["no ship serial: FRU 0 Product Serial empty — do not ship"]


def test_population_no_ship_serial_is_red_without_a_profile():
    out = host_qual.population_check("12 Memory Size: 64 GB", None, baseboard=_NO_SERIAL)
    assert out["verdict"] == "red" and out["no_ship_serial"] is True


def test_population_no_ship_serial_names_the_leopard_field():
    out = host_qual.population_check("x", None, baseboard=dict(_NO_SERIAL, serial_field="Chassis Serial"))
    assert out["failed_rules"] == ["no ship serial: FRU 0 Chassis Serial empty — do not ship"]


def test_population_waits_for_the_inventory_dump():
    out = host_qual.population_check(None, "ovh-bare-tp", baseboard=_NO_SERIAL)
    assert out["verdict"] == "grey" and "no_ship_serial" not in out


def test_population_ok_absent_and_prerelease_baseboards_add_no_rule(monkeypatch):
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: ["12 Memory Size: 64 GB"])
    for bb in ({"state": "ok", "serial": "WTH1"}, {"state": "absent"},
               {"board_mfg": "Wistron", "product": "TP", "serial": "1I02270LSS"}, None):
        out = host_qual.population_check("12 Memory Size: 64 GB", "ovh-bare-tp", baseboard=bb)
        assert out["verdict"] == "green" and "no_ship_serial" not in out, bb


def test_population_no_ship_serial_and_blocked_dimms_both_listed(monkeypatch):
    monkeypatch.setattr(host_qual.blocklist, "check_dimmsum", lambda d: [{"slot": "A1"}])
    monkeypatch.setattr(host_qual.blocklist, "describe", lambda h: "blocked DIMM A1")
    out = host_qual.population_check("x", None, dimmsum="d", baseboard=_NO_SERIAL)
    assert out["verdict"] == "red"
    assert out["failed_rules"] == ["blocked DIMM A1", "no ship serial: FRU 0 Product Serial empty — do not ship"]


def test_poll_passes_the_ladder_baseboard_to_population(monkeypatch):
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: None)
    # keep the verdict tail (identify / power / durable result) out of this wiring test
    monkeypatch.setattr(host_qual, "qualify_verdict", lambda status, pop, checks=None: None)
    agent = FakeAgent(stages=[{"name": "inventory", "status": "pass"}],
                      status_body={"status": "running", "current": "inventory",
                                   "done_n": 1, "total_n": 11, "verdict": None, "pct": 9},
                      arts={("inventory", "macinv"): "12 Memory Size: 64 GB"})
    store = _store()
    store.state[TARGET["port"]] = {"ladder": {"bmc_fru": _NO_SERIAL}}
    qual = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert store.state[TARGET["port"]]["pop"]["no_ship_serial"] is True
    assert qual["steps"]["population-check"]["status"] == "fail"


def test_qualify_verdict_pass_needs_node_pass_and_green():
    assert host_qual.qualify_verdict({"verdict": "pass"}, {"verdict": "green"}) == "pass"
    assert host_qual.qualify_verdict({"verdict": "pass"}, {"verdict": "red"}) == "fail"
    assert host_qual.qualify_verdict({"verdict": "fail"}, {"verdict": "green"}) == "fail"
    assert host_qual.qualify_verdict({"verdict": None}, {"verdict": "grey"}) is None


def _power_seq(*statuses):
    """A fake actions.run_power: 'off' is accepted, each 'status' call pops the next reading."""
    calls, readings = [], list(statuses)
    def powr(ip, action, **k):
        calls.append(action)
        if action == "status":
            r = readings.pop(0) if readings else "unreadable"
            return {"ok": r != "unreadable", "action": "status", "ip": ip,
                    "output": "" if r == "unreadable" else f"Chassis Power is {r}"}
        return {"ok": True, "action": action, "ip": ip, "output": ""}
    return powr, calls


def test_run_done_pass_forces_identify_then_powers_off_and_verifies():
    idcalls = []
    ident = lambda ip, mode, **k: idcalls.append((ip, mode)) or {"ok": True}
    powr, calls = _power_seq("on", "off")
    done = host_qual.run_done(TARGET, "pass", identify=ident, power=powr, sleep=lambda s: None)
    assert idcalls == [("10.0.0.6", "force")]
    assert calls == ["off", "status", "status"]
    assert done == {"identify": "done", "power_off": "done", "verdict": "pass"}


def test_run_done_records_fault_when_node_stays_on():
    powr, calls = _power_seq(*(["on"] * 6))
    done = host_qual.run_done(TARGET, "pass", identify=lambda *a, **k: {"ok": True},
                              power=powr, sleep=lambda s: None)
    assert done["power_off"] == "fault" and done["power_off_reason"] == "still_on"
    assert done["verdict"] == "pass"
    assert calls.count("status") == 6


def test_run_done_records_fault_when_power_unreadable():
    powr, _ = _power_seq(*(["unreadable"] * 6))
    done = host_qual.run_done(TARGET, "pass", identify=lambda *a, **k: {"ok": True},
                              power=powr, sleep=lambda s: None)
    assert done["power_off"] == "fault" and done["power_off_reason"] == "unreadable"


def test_run_done_identify_failure_is_recorded_not_fatal():
    powr, _ = _power_seq("off")
    done = host_qual.run_done(TARGET, "pass", identify=lambda *a, **k: {"ok": False},
                              power=powr, sleep=lambda s: None)
    assert done["identify"] == "fault" and done["power_off"] == "done"


def test_run_done_fail_leaves_node_powered():
    powr, calls = _power_seq()
    done = host_qual.run_done(TARGET, "fail", identify=lambda *a, **k: {"ok": True}, power=powr)
    assert calls == []                      # no power-off on fail
    assert done == {"verdict": "fail"}


def test_restart_purges_old_run_then_acks_in_order():
    events = []
    class Agent:
        def restart(self): events.append("restart"); return {"old_run_id": "r1", "new_run_id": "r2"}
        def restart_ack(self): events.append("ack"); return {"ok": True}
    class Store:
        def purge_run(self, mac, run_id): events.append(("purge", mac, run_id))
        def set_state(self, port, **kw): events.append(("clear", port, kw))
    out = host_qual.restart_target(TARGET, make_client=lambda ip: Agent(), store=Store())
    assert out["ok"] is True and out["new_run_id"] == "r2"
    # purge (old run) must happen BEFORE ack (the commit point)
    assert events.index(("purge", "aa:bb", "r1")) < events.index("ack")
    assert events[0] == "restart"


def test_restart_unreachable_does_not_purge():
    from flax_post.qualclient import QualUnreachable
    purged = []
    class Agent:
        def restart(self): raise QualUnreachable("down")
    class Store:
        def purge_run(self, mac, run_id): purged.append(run_id)
        def set_state(self, *a, **k): pass
    out = host_qual.restart_target(TARGET, make_client=lambda ip: Agent(), store=Store())
    assert out["ok"] is False and purged == []


def test_poll_pass_verdict_runs_done_and_writes_vars_done(monkeypatch):
    # integration: a pass verdict (node pass + green pop) runs Done via poll_target.
    # run_done's identify/power keyword defaults are bound to actions.run_identify/
    # run_power at host_qual import time, so patching those module attributes here
    # is a no-op (early-bound defaults) -- patch the actual subprocess seam
    # (actions.RUNNER, per actions.py's documented test seam) instead.
    from flax_post.observe import host_qual
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: [])  # no rules -> green
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: "prof")
    calls = []
    monkeypatch.setattr(host_qual.actions, "RUNNER",
                        lambda argv, timeout: calls.append(argv) or (0, "Chassis Power is off"))
    agent = FakeAgent(status_body={"status": "done", "current": None, "done_n": 11,
                                   "total_n": 11, "verdict": "pass", "pct": 100},
                      stages=[{"name": "inventory", "status": "pass"}],
                      arts={("inventory", "macinv"): "12 Memory Size: 64 GB", **GOOD_ARTS})
    store = _store()
    qual = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert store.state["et6b1"]["done"]["verdict"] == "pass"
    assert ["powertriage", "10.0.0.6", "off"] in calls
    assert qual["done"]["verdict"] == "pass"


def test_poll_unreachable_after_pass_preserves_green_tile():
    from flax_post.observe import host_qual
    from flax_post.qualclient import QualUnreachable
    store = _store()
    store.set_state("et6b1", done={"verdict": "pass", "identify": "done", "power_off": "done"},
                    qual={"run_id": "r1", "agent": {"reachable": True},
                          "steps": {"sel-post": {"status": "pass"}}})
    class Dead:
        def health(self): raise QualUnreachable("off")
    qual = host_qual.poll_target(TARGET, make_client=lambda ip: Dead(), store=store)
    assert qual["steps"]["sel-post"]["status"] == "pass"                       # not clobbered
    assert store.state["et6b1"]["qual"]["steps"]["sel-post"]["status"] == "pass"
    assert store.state["et6b1"]["done"]["verdict"] == "pass"


# --- engine-triggered qualify launch (Wave-4 (a)) ---
class _Dead:
    def health(self):
        from flax_post.qualclient import QualUnreachable
        raise QualUnreachable("down")


def test_poll_launches_agent_when_firmware_done_and_unreachable():
    launched, store = [], _store()
    tgt = dict(TARGET, phase="Qualify")                 # Discover+Firmware done, Qualify pending
    host_qual.poll_target(tgt, make_client=lambda ip: _Dead(), store=store,
                          launch_agent=lambda t: launched.append(t["port"]))
    assert launched == ["et6b1"]


def test_poll_no_launch_while_still_in_firmware():
    launched, store = [], _store()
    tgt = dict(TARGET, phase="Firmware")                # still flashing -> must NOT start qualify
    host_qual.poll_target(tgt, make_client=lambda ip: _Dead(), store=store,
                          launch_agent=lambda t: launched.append(t["port"]))
    assert launched == []


def test_poll_debounces_relaunch_within_cooldown():
    # Per-poll relaunches stomp a still-starting agent; a second poll inside the
    # cooldown must NOT relaunch (it also records launch_at so the debounce persists).
    launched, store = [], _store()
    tgt = dict(TARGET, phase="Qualify")
    clock = [1000.0]
    la = lambda t: launched.append(t["port"])
    host_qual.poll_target(tgt, make_client=lambda ip: _Dead(), store=store,
                          launch_agent=la, now=lambda: clock[0])
    clock[0] += 30                                      # well inside the cooldown
    host_qual.poll_target(tgt, make_client=lambda ip: _Dead(), store=store,
                          launch_agent=la, now=lambda: clock[0])
    assert launched == ["et6b1"]                        # launched once, not twice
    assert store.state["et6b1"]["launch_at"] == 1000.0  # attempt time recorded


def test_poll_relaunches_after_cooldown():
    # A launch that didn't take (host mid-reboot) is retried, but only after the
    # cooldown -- gentle backoff, never a per-poll hammer.
    launched, store = [], _store()
    tgt = dict(TARGET, phase="Qualify")
    clock = [1000.0]
    la = lambda t: launched.append(t["port"])
    host_qual.poll_target(tgt, make_client=lambda ip: _Dead(), store=store,
                          launch_agent=la, now=lambda: clock[0])
    clock[0] += host_qual.LAUNCH_COOLDOWN_S + 1         # past the cooldown
    host_qual.poll_target(tgt, make_client=lambda ip: _Dead(), store=store,
                          launch_agent=la, now=lambda: clock[0])
    assert launched == ["et6b1", "et6b1"]               # retried once cooldown elapsed


def test_poll_no_launch_when_agent_already_reachable():
    launched, store = [], _store()
    agent = FakeAgent(status_body={"status": "running", "current": "sdr-pre", "done_n": 0,
                                   "total_n": 12, "verdict": None, "pct": 0},
                      stages=[{"name": "sdr-pre", "status": "running"}])
    tgt = dict(TARGET, phase="Qualify")
    host_qual.poll_target(tgt, make_client=lambda ip: agent, store=store,
                          launch_agent=lambda t: launched.append(t["port"]))
    assert launched == []                               # agent up -> poll, don't relaunch


def test_launch_script_idempotent_and_starts_postautomate():
    sh = host_qual._LAUNCH_SH
    assert "is-active --quiet flax-qual-agent && exit 0" in sh   # no-op if already running
    assert "curl -sf http://bang/post.tgz" in sh                 # refresh payload (not banghook)
    assert "./post.sh postautomate" in sh


# ---- skip capture + durable result (spec 2026-09-11-post-completion-latch) ----
def test_skip_stage_summary_is_captured():
    store = _store()
    agent = FakeAgent(status_body={"status": "running", "current": "iperf", "done_n": 6,
                                   "total_n": 12, "verdict": None, "pct": 50},
                      stages=[{"name": "fio", "status": "skip"}],
                      summaries={"fio": {"reason": "no physical storage media"}})
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    fio = store.state["et6b1"]["qual"]["steps"]["fio"]
    assert fio["status"] == "skip"
    assert fio["summary"] == {"reason": "no physical storage media"}


def test_build_result_shape():
    live = {"fw_bmc": {"phase": "up_to_date", "current": "1.1.2", "target": "1.1.2"},
            "fw_bios": {"phase": "up_to_date", "current": "TPC_P26F", "target": "TPC_P26F"},
            "fw_nic": {"phase": "up_to_date", "devices": []}}
    qual = {"run_id": "r1", "overall": {"verdict": "pass"},
            "steps": {"fio": {"status": "skip", "summary": {"reason": "x"}, "artifacts": [{"name": "a"}]}}}
    pop = {"profile": "p", "verdict": "green", "failed_rules": []}
    done = {"verdict": "pass", "identify": "done", "power_off": "done"}
    res = host_qual.build_result(dict(TARGET, order_no="RRORD-100", serial="SN1"),
                                 live, qual, pop, done, now=lambda: 1000)
    assert res["run_id"] == "r1" and res["verdict"] == "pass" and res["finished_at"] == 1000
    assert res["order_no"] == "RRORD-100" and res["port"] == "et6b1" and res["serial"] == "SN1"
    assert res["fw"]["bios"]["current"] == "TPC_P26F"
    assert res["qual"]["steps"]["fio"] == {"status": "skip", "summary": {"reason": "x"}}
    assert res["pop"] == pop and res["done"] == done


def test_poll_records_result_once_per_run(monkeypatch):
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: [])
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: "prof")
    runner_calls = []
    monkeypatch.setattr(host_qual.actions, "RUNNER",
                        lambda argv, timeout: runner_calls.append(argv) or (0, "Chassis Power is off"))
    store = _store()
    recorded = []
    store.record_result = lambda mac, result, **kw: recorded.append((mac, result["run_id"], kw))
    agent = FakeAgent(status_body={"status": "done", "current": None, "done_n": 12,
                                   "total_n": 12, "verdict": "pass", "pct": 100},
                      stages=[{"name": "inventory", "status": "pass"}],
                      arts={("inventory", "macinv"): "12 Memory Size: 64 GB", **GOOD_ARTS})
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)   # same run again
    assert len(recorded) == 1
    mac, run_id, kw = recorded[0]
    assert mac == "aa:bb" and run_id == "r1"
    assert kw["last_port"] == "et6b1" and kw["serial"] == "SN1" and kw["order_no"] == "RRORD-003"
    # the Done tail ran exactly once as well: one identify + one power off
    assert sum(1 for a in runner_calls if a[:1] == ["powertriage"] and a[-1] == "off") == 1
    assert sum(1 for a in runner_calls if a[:1] == ["identtriage"]) == 1


def test_poll_unreachable_after_fail_preserves_failed_steps():
    """A failed blade that is later powered off must keep its qualify evidence on
    the tile (which step failed), exactly like a passed one keeps its green."""
    from flax_post.qualclient import QualUnreachable
    store = _store()
    store.set_state("et6b1", done={"verdict": "fail"},
                    qual={"run_id": "r1", "agent": {"reachable": True},
                          "steps": {"iperf": {"status": "fail"}}})
    class Dead:
        def health(self): raise QualUnreachable("off")
    launched = []
    qual = host_qual.poll_target(dict(TARGET, phase="Qualify"), make_client=lambda ip: Dead(),
                                 store=store, launch_agent=lambda t: launched.append(t))
    assert qual["steps"]["iperf"]["status"] == "fail"
    assert store.state["et6b1"]["qual"]["steps"]["iperf"]["status"] == "fail"
    assert launched == []          # no relaunch of a failed blade until its latch clears


def test_poll_skips_network_when_row_has_verdict():
    calls = []
    def make_client(ip):
        calls.append(ip); return FakeAgent()
    store = _store()
    store.state["et6b1"] = {"done": {"verdict": "pass"}, "qual": {"run_id": "r0"}}
    out = host_qual.poll_target(TARGET, make_client=make_client, store=store)
    assert calls == [] and out == {"run_id": "r0"}


def test_poll_skips_network_when_powered_off():
    calls = []
    store = _store()
    store.state["et6b1"] = {"power_on": "off", "qual": {"agent": {"reachable": False}}}
    out = host_qual.poll_target(TARGET, make_client=lambda ip: calls.append(ip), store=store)
    assert calls == [] and out == {"agent": {"reachable": False}}


def test_poll_pings_before_health_and_marks_unreachable_without_launch():
    launched = []
    store = _store()
    target = dict(TARGET, phase="Qualify")
    out = host_qual.poll_target(target, make_client=lambda ip: FakeAgent(), store=store,
                                launch_agent=lambda t: launched.append(t["port"]),
                                ping=lambda ip: False)
    assert out["agent"]["reachable"] is False and launched == []
    assert store.state["et6b1"]["qual"]["steps"]["agent-reachable"]["status"] == "pending"


_PASS_AGENT = dict(status_body={"status": "done", "verdict": "pass", "done_n": 1,
                                "total_n": 1, "pct": 100}, stages=[])


def _with_forced_pass(monkeypatch):
    """The fake store has no population profile, so qualify_verdict would stay
    None; force the verdict and stub the BMC-touching Done tail."""
    import flax_post.observe.host_qual as hq
    monkeypatch.setattr(hq, "qualify_verdict", lambda status, pop, checks=None: "pass")
    monkeypatch.setattr(hq, "run_done",
                        lambda *a, **k: {"identify": "done", "power_off": "done", "verdict": "pass"})


def test_console_artifact_stored_once_at_verdict(monkeypatch):
    _with_forced_pass(monkeypatch)
    agent = FakeAgent(**_PASS_AGENT)
    store = _store()
    reads = []
    def reader(bmc_ip):
        reads.append(bmc_ip); return "=== flax-post mark 2026-09-11T14:00:00Z power-on ===\nPOST..."
    q = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store, console_reader=reader)
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store, console_reader=reader)
    assert reads == ["10.0.0.6"]                                   # once: the 2nd poll is latched
    assert store.arts[("aa:bb", "r1", "console", "sol.txt")].startswith("=== flax-post mark")
    assert q["steps"]["console"] == {"status": "pass"}


def test_console_missing_records_fault_step_not_verdict(monkeypatch):
    _with_forced_pass(monkeypatch)
    agent = FakeAgent(**_PASS_AGENT)
    store = _store()
    q = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store,
                              console_reader=lambda ip: None)
    assert q["steps"]["console"] == {"status": "fail", "summary": {"reason": "no sol capture"}}
    assert q["done"]["verdict"] == "pass"


_REC_STEPS = {"Discover": {"power-on": "done", "tftp-seen": "done"},
              "Firmware": {"bmc-checked": "done", "bios-updated": "done"},
              "Qualify": {"fio": "skip"}, "Done": {"done": "pending"}}


def test_verdict_snapshots_discover_and_firmware_into_done(monkeypatch):
    """The worker hands poll_target the blade record's step maps; the verdict
    write latches Discover + Firmware (ruling 2026-09-12) so power-off cannot
    regress them on the tile. Qualify/Done are not snapshotted."""
    _with_forced_pass(monkeypatch)
    agent = FakeAgent(**_PASS_AGENT)
    store = _store()
    host_qual.poll_target(dict(TARGET, steps=_REC_STEPS), make_client=lambda ip: agent,
                          store=store, console_reader=lambda ip: "cap")
    done = store.state["et6b1"]["done"]
    assert done["verdict"] == "pass"
    assert done["steps"] == {"Discover": _REC_STEPS["Discover"], "Firmware": _REC_STEPS["Firmware"]}


def test_verdict_snapshot_is_taken_once_per_run(monkeypatch):
    _with_forced_pass(monkeypatch)
    agent = FakeAgent(**_PASS_AGENT)
    store = _store()
    host_qual.poll_target(dict(TARGET, steps=_REC_STEPS), make_client=lambda ip: agent, store=store)
    later = {"Discover": {"power-on": "cur"}, "Firmware": {}}
    host_qual.poll_target(dict(TARGET, steps=later), make_client=lambda ip: agent, store=store)
    assert store.state["et6b1"]["done"]["steps"]["Discover"] == _REC_STEPS["Discover"]


def test_verdict_without_record_steps_writes_no_snapshot(monkeypatch):
    _with_forced_pass(monkeypatch)
    agent = FakeAgent(**_PASS_AGENT)
    store = _store()
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert "steps" not in store.state["et6b1"]["done"]


def test_launch_script_retries_the_payload_fetch_and_fails_loudly():
    """12 s after sshd answered, `curl http://bang/post.tgz` failed once on
    et27b4 (2026-09-12) and, under `set -e` inside an && list, the script
    silently fell through to `./post.sh` -> rc=127 with no agent. The fetch
    must retry for a while and a final failure must exit non-zero BEFORE
    post.sh, with a message naming the fetch."""
    sh = host_qual._LAUNCH_SH
    assert "for " in sh and "curl -sf http://bang/post.tgz" in sh
    assert sh.index("curl -sf") < sh.index("./post.sh postautomate")
    assert "post.tgz fetch failed" in sh
    assert sh.index("post.tgz fetch failed") < sh.index("./post.sh postautomate")


def test_launch_script_creates_the_hook_dir_before_cd():
    """The ladder launches seconds after sshd is up, before banghook has made
    /opt/flax/hook (et8b4, 2026-09-11)."""
    assert "mkdir -p /opt/flax/hook && cd /opt/flax/hook" in host_qual._LAUNCH_SH
    assert host_qual._LAUNCH_SH.index("mkdir -p") < host_qual._LAUNCH_SH.index("curl")


def test_console_store_failure_does_not_lose_the_result_or_the_qual_write(monkeypatch):
    """et10b2 2026-09-12: write_artifact raised on the console capture and the
    exception escaped after done was written -> no console step, no durable
    result, live qual stale at 'running sdr-post' under a pass verdict."""
    _with_forced_pass(monkeypatch)
    agent = FakeAgent(status_body={"status": "done", "verdict": "pass", "done_n": 1, "total_n": 1, "pct": 100},
                      stages=[{"name": "sel-post", "status": "pass"}])
    store = _store()
    recorded = []
    store.record_result = lambda mac, result, **kw: recorded.append(result["run_id"])
    real_write = store.write_artifact
    def flaky(bmc_mac, run_id, stage, name, *a, **kw):
        if stage == "console":
            raise RuntimeError("PostgreSQL text fields cannot contain NUL")
        return real_write(bmc_mac, run_id, stage, name, *a, **kw)
    store.write_artifact = flaky
    q = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store, console_reader=lambda ip: "cap")
    assert store.state["et6b1"]["done"]["verdict"] == "pass"
    assert q["steps"]["console"]["status"] == "fail"
    assert "store" in q["steps"]["console"]["summary"]["reason"]
    assert q["steps"]["sel-post"]["status"] == "pass"
    assert store.state["et6b1"]["qual"]["steps"]["sel-post"]["status"] == "pass"   # final write happened
    assert recorded == ["r1"]


def test_build_result_records_clean_holes_and_the_boot_timeline():
    from flax_post import blades
    snap = {"Discover": {n: "done" for n in blades.PHASE_STEPS["Discover"]},
            "Firmware": {n: "done" for n in blades.PHASE_STEPS["Firmware"]}}
    qual = {"run_id": "r1", "overall": {"verdict": "pass"},
            "steps": {n: {"status": "pass"} for n in blades.PHASE_STEPS["Qualify"]}}
    qual["steps"]["fio"] = {"status": "skip", "summary": {"reason": "no physical storage media"}}
    pop = {"profile": "p", "verdict": "green", "failed_rules": []}
    done = {"verdict": "pass", "identify": "done", "power_off": "done", "steps": snap}
    live = {"ladder": {"power_on_at": 10.0, "marks": {"tftp": 20.0, "ssh": 90.0}}}
    res = host_qual.build_result(TARGET, live, qual, pop, done, now=lambda: 1000)
    assert res["clean"] is True and res["holes"] == {}
    assert res["ladder"] == {"power_on_at": 10.0, "marks": {"tftp": 20.0, "ssh": 90.0}, "human_power_on": False}
    # a hole anywhere -> not clean, and the holes are named
    dirty = dict(done, steps={"Discover": dict(snap["Discover"], **{"tftp-seen": "unknown"}), "Firmware": snap["Firmware"]})
    res = host_qual.build_result(TARGET, live, qual, pop, dirty, now=lambda: 1000)
    assert res["clean"] is False and res["holes"] == {"Discover": ["tftp-seen"]}
    # no snapshot -> nothing to judge
    res = host_qual.build_result(TARGET, live, qual, pop, {"verdict": "pass"}, now=lambda: 1000)
    assert res["clean"] is None and res["holes"] == {}


# ---- post-verdict collection (ruling 2026-09-12) ----
def _fail_latched_store(status="running"):
    store = _store()
    store.set_state("et6b1", power_on="on", pop={"profile": "p", "verdict": "red", "failed_rules": ["12 Memory Size: 64 GB"]},
                    done={"verdict": "fail", "steps": {"Discover": {}, "Firmware": {}}},
                    qual={"run_id": "r1", "agent": {"reachable": True},
                          "overall": {"status": status, "current": "iperf", "done_n": 6, "total_n": 12, "verdict": None},
                          "steps": {"inventory": {"status": "pass"}, "iperf": {"status": "running"}}})
    return store


def test_fail_verdict_keeps_polling_and_records_later_stages(monkeypatch):
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: ["12 Memory Size: 64 GB"])
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: "p")
    store = _fail_latched_store()
    launched = []
    agent = FakeAgent(status_body={"status": "running", "current": "mem-post", "done_n": 9, "total_n": 12, "verdict": None, "pct": 75},
                      stages=[{"name": "inventory", "status": "pass"}, {"name": "iperf", "status": "pass"},
                              {"name": "mem-pre", "status": "pass"}, {"name": "cpu-mem-stress", "status": "fail"},
                              {"name": "mem-post", "status": "running"}],
                      arts={("inventory", "macinv"): "11 Memory Size: 64 GB", ("cpu-mem-stress", "stress"): "boom"})
    q = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store, launch_agent=lambda t: launched.append(t))
    assert q["steps"]["iperf"]["status"] == "pass" and q["steps"]["cpu-mem-stress"]["status"] == "fail"
    assert store.state["et6b1"]["qual"]["steps"]["cpu-mem-stress"]["status"] == "fail"
    assert store.arts[("aa:bb", "r1", "cpu-mem-stress", "stress")] == "boom"     # later artifacts captured
    assert store.state["et6b1"]["done"]["verdict"] == "fail"                       # the verdict never moves
    assert launched == []                                                          # never relaunched


def test_fail_verdict_transient_miss_does_not_wipe_collected_steps():
    from flax_post.qualclient import QualUnreachable
    store = _fail_latched_store()
    class Dead:
        def health(self): raise QualUnreachable("rebooting")
    launched = []
    q = host_qual.poll_target(TARGET, make_client=lambda ip: Dead(), store=store, launch_agent=lambda t: launched.append(t))
    assert q["steps"]["inventory"]["status"] == "pass" and launched == []
    assert store.state["et6b1"]["qual"]["steps"]["inventory"]["status"] == "pass"


def test_fail_verdict_battery_finish_updates_the_durable_result_in_place_once(monkeypatch):
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: ["12 Memory Size: 64 GB"])
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: "p")
    store = _fail_latched_store()
    updated, recorded = [], []
    store.update_result = lambda mac, result: updated.append(result)
    store.record_result = lambda mac, result, **kw: recorded.append(result)
    agent = FakeAgent(status_body={"status": "done", "current": None, "done_n": 12, "total_n": 12, "verdict": "pass", "pct": 100},
                      stages=[{"name": n, "status": "pass"} for n in ("inventory", "iperf", "mem-pre", "cpu-mem-stress", "mem-post", "sdr-post", "sel-post")],
                      arts={("inventory", "macinv"): "11 Memory Size: 64 GB"})
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert recorded == [] and len(updated) == 1
    assert updated[0]["verdict"] == "fail" and updated[0]["qual"]["steps"]["sel-post"]["status"] == "pass"
    # terminal now: the next poll is a no-op
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert len(updated) == 1


def test_pass_verdict_still_returns_early():
    store = _store()
    store.set_state("et6b1", power_on="off", done={"verdict": "pass"}, qual={"run_id": "r1", "overall": {"status": "running"}})
    calls = []
    class Loud:
        def health(self): calls.append(1); return {}
    host_qual.poll_target(TARGET, make_client=lambda ip: Loud(), store=store)
    assert calls == []


def test_fail_verdict_battery_finish_captures_the_console_and_keeps_the_console_step(monkeypatch):
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: ["12 Memory Size: 64 GB"])
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: "p")
    store = _fail_latched_store()
    store.update_result = lambda mac, result: None
    running = FakeAgent(status_body={"status": "running", "current": "iperf", "done_n": 6, "total_n": 12, "verdict": None, "pct": 50},
                        stages=[{"name": "inventory", "status": "pass"}, {"name": "iperf", "status": "running"}],
                        arts={("inventory", "macinv"): "11 Memory Size: 64 GB"})
    store.state["et6b1"]["qual"]["steps"]["console"] = {"status": "pass"}       # recorded at the verdict
    q = host_qual.poll_target(TARGET, make_client=lambda ip: running, store=store, console_reader=lambda ip: "early")
    assert q["steps"]["console"] == {"status": "pass"}                            # carried over, not lost
    finished = FakeAgent(status_body={"status": "done", "current": None, "done_n": 12, "total_n": 12, "verdict": "pass", "pct": 100},
                         stages=[{"name": n, "status": "pass"} for n in ("inventory", "iperf", "sel-post")],
                         arts={("inventory", "macinv"): "11 Memory Size: 64 GB"})
    q = host_qual.poll_target(TARGET, make_client=lambda ip: finished, store=store, console_reader=lambda ip: "the whole boot to the end")
    assert store.arts[("aa:bb", "r1", "console", "sol.txt")] == "the whole boot to the end"   # captured to the end
    assert q["steps"]["console"] == {"status": "pass"}


# --- ship checks: smbios-serial + tpm-state (spec 2026-10-29) ---------------
GOOD_FRU = "FRU Device Description : Builtin FRU Device (ID 0)\n Product Serial        : SN1\n"
GOOD_DMI = "System Information\n\tSerial Number: SN1\n"
GOOD_TPM = ("device=present\nowned=0 perm=0x00000000 persistent=0 startup=0x80000006 "
            "shEnable=1 ehEnable=1 phEnable=0\n")
GOOD_ARTS = {("inventory", "ipmitool-fru"): GOOD_FRU, ("inventory", "dmidecode"): GOOD_DMI,
             ("inventory", "tpmstate"): GOOD_TPM}


def _inv_agent(inv_status="pass", arts=None, overall=None):
    return FakeAgent(stages=[{"name": "inventory", "status": inv_status}],
                     status_body=overall or {"status": "running", "current": "fio", "done_n": 1,
                                             "total_n": 11, "verdict": None, "pct": 9},
                     arts={("inventory", "macinv"): "12 Memory Size: 64 GB", **(arts or {})})


def test_poll_writes_checks_slice_and_steps():
    store = _store()
    qual = host_qual.poll_target(TARGET, make_client=lambda ip: _inv_agent(arts=GOOD_ARTS), store=store)
    checks = store.state["et6b1"]["checks"]
    assert checks["tpm-state"]["status"] == "pass" and checks["smbios-serial"]["status"] == "skip"
    assert checks["smbios-serial"]["judged"] == "pass"
    assert qual["steps"]["smbios-serial"] == {"status": "skip", "summary": {"reason": "disabled"}}
    assert qual["steps"]["tpm-state"] == {"status": "pass", "summary": {"reason": ""}}


def test_checks_pending_until_inventory_terminal():
    store = _store()
    qual = host_qual.poll_target(TARGET, make_client=lambda ip: _inv_agent("running", GOOD_ARTS), store=store)
    assert {n: c["status"] for n, c in store.state["et6b1"]["checks"].items()} == \
        {"smbios-serial": "pending", "tpm-state": "pending"}
    assert qual["steps"]["tpm-state"]["status"] == "pending"


def test_checks_run_when_inventory_failed():
    store = _store()
    host_qual.poll_target(TARGET, make_client=lambda ip: FakeAgent(
        stages=[{"name": "inventory", "status": "fail"}], status_body={"status": "running"}), store=store)
    checks = store.state["et6b1"]["checks"]
    assert (checks["smbios-serial"]["status"], checks["smbios-serial"]["judged"]) == ("skip", "fail: no ipmitool-fru artifact")
    assert (checks["tpm-state"]["status"], checks["tpm-state"]["reason"]) == ("fail", "no tpmstate artifact")


def test_failed_check_fails_the_run(monkeypatch):
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: [])   # green
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: "prof")
    arts = {**GOOD_ARTS, ("inventory", "tpmstate"): "device=absent\n"}
    agent = _inv_agent(arts=arts, overall={"status": "done", "current": None, "done_n": 11,
                                           "total_n": 11, "verdict": "pass", "pct": 100})
    store = _store()
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert store.state["et6b1"]["done"]["verdict"] == "fail"


def test_disabled_smbios_serial_does_not_fail_the_run(monkeypatch):
    # the Quanta case: FRU serial present, SMBIOS left at the BIOS default
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: [])   # green
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: "prof")
    arts = {**GOOD_ARTS, ("inventory", "dmidecode"): "System Information\n\tSerial Number: To be filled by O.E.M.\n"}
    agent = _inv_agent(arts=arts, overall={"status": "done", "current": None, "done_n": 11,
                                           "total_n": 11, "verdict": "pass", "pct": 100})
    store = _store()
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    c = store.state["et6b1"]["checks"]["smbios-serial"]
    assert (c["status"], c["judged"]) == ("skip", "fail: SN SN1 in FRU 0, not in SMBIOS")
    assert store.state["et6b1"]["done"]["verdict"] == "pass"
    assert "judged: fail: SN SN1" in store.arts[(TARGET["bmc_mac"], "r1", "smbios-serial", "check")]


def test_qualify_verdict_with_checks():
    ok = {"smbios-serial": {"status": "pass"}, "tpm-state": {"status": "pass"}}
    bad = dict(ok, **{"tpm-state": {"status": "fail"}})
    wait = dict(ok, **{"smbios-serial": {"status": "pending"}})
    v = host_qual.qualify_verdict
    assert v({"verdict": "pass"}, {"verdict": "green"}, ok) == "pass"
    assert v({"verdict": "pass"}, {"verdict": "green"}, bad) == "fail"
    assert v({"verdict": None}, {"verdict": "grey"}, bad) == "fail"
    assert v({"verdict": "pass"}, {"verdict": "green"}, wait) is None
    assert v({"verdict": "pass"}, {"verdict": "green"}, {}) is None          # a check never judged
    assert v({"verdict": "pass"}, {"verdict": "green"}, dict(ok, **{"smbios-serial": {"status": "skip"}})) == "pass"
    assert v({"verdict": "pass"}, {"verdict": "green"}) == "pass"            # old callers unchanged


def test_restart_clears_checks():
    seen = {}
    class Agent:
        def restart(self): return {"old_run_id": "r1", "new_run_id": "r2"}
        def restart_ack(self): return {"ok": True}
    class Store:
        def purge_run(self, mac, run_id): pass
        def set_state(self, port, **kw): seen.update(kw)
    host_qual.restart_target(TARGET, make_client=lambda ip: Agent(), store=Store())
    assert seen["checks"] == {}


def test_build_result_carries_checks():
    checks = {"smbios-serial": {"status": "pass", "reason": ""}, "tpm-state": {"status": "fail", "reason": "no TPM device"}}
    snap = {"Discover": {}, "Firmware": {}}
    r = host_qual.build_result(TARGET, {}, {"run_id": "r1", "steps": {}}, {"verdict": "green"},
                               {"verdict": "fail", "steps": snap}, checks=checks, now=lambda: 1)
    assert r["checks"] == checks


def test_ship_checks_stamp_the_checker_version():
    from flax_post.version import __version__
    store = _store()
    host_qual.poll_target(TARGET, make_client=lambda ip: _inv_agent(arts=GOOD_ARTS), store=store)
    for c in store.state["et6b1"]["checks"].values():
        assert c["checker"] == "flax-post " + __version__


def test_poll_stores_ship_check_evidence_once_per_run():
    from flax_post.version import __version__
    store = _store()
    writes = []
    real = store.write_artifact
    store.write_artifact = lambda *a, **k: writes.append(a[2:4]) or real(*a, **k)
    for _ in range(2):
        host_qual.poll_target(TARGET, make_client=lambda ip: _inv_agent(arts=GOOD_ARTS), store=store)
    t = store.arts[("aa:bb", "r1", "tpm-state", "check")]
    assert "checked by: flax-post " + __version__ in t and "status: pass" in t and "shEnable=1" in t
    assert "judged: pass" in store.arts[("aa:bb", "r1", "smbios-serial", "check")]
    assert writes.count(("tpm-state", "check")) == 1 and writes.count(("smbios-serial", "check")) == 1


def test_pending_ship_checks_store_no_evidence():
    store = _store()
    host_qual.poll_target(TARGET, make_client=lambda ip: _inv_agent("running", GOOD_ARTS), store=store)
    assert not [k for k in store.arts if k[2] in ("tpm-state", "smbios-serial")]


# ---- fio judge (spec 2026-11-04 fio baselines: Post loose pipeline) ----
from flax_rack.fakes import _fio_job

_HDD = "ST6000NM019B-2TG103"
_HDD_ALLDISKS = "".join(
    f"/dev/{d} [0:0:{i}:0] Front_ArrayDevice0{i} mpt3sas rotational 6001.2 "
    f"SEAGATE SN04 {_HDD} WX01DSV{i}\n" for i, d in enumerate(("sda", "sdb", "sdc"), 2))
_HDD_BASE = {"suite": "v1", "groups": [
    {"model": _HDD[:16], "size_gb": 6001.2, "controller": "mpt3sas", "job": "lat", "metric": "lat_us",
     "n": 40, "mean": 5000.0, "std": 100.0}]}


def _hdd_fio(lat_by_dev):
    return "\n".join(_fio_job(d, *j) for d, lat in lat_by_dev.items() for j in (
        ("bw", "read", "64.0KiB", 64, "3700", 231, 242, "69000", "usec", 70000, "usec"),
        ("lat", "randread", "4096B", 1, "190", 0.7, 0.8, lat, "usec", 9000, "usec"),
        ("iops", "read", "4096B", 256, "60.1k", 230, 241, "4200", "usec", 5000, "usec")))


def _fio_agent(fio_status="pass"):
    return FakeAgent(status_body={"status": "running", "current": "iperf", "done_n": 6,
                                  "total_n": 12, "verdict": None, "pct": 50},
                     stages=[{"name": "inventory", "status": "pass"},
                             {"name": "fio", "status": fio_status}],
                     arts={("inventory", "alldisks"): _HDD_ALLDISKS,
                           ("fio", "fio"): _hdd_fio({"sda": "5010", "sdb": "4990", "sdc": "9000"})},
                     summaries={"fio": {"devices": ["sda", "sdb", "sdc"], "smart_excluded": {}}})


def test_poll_target_judges_fio_once():
    store, agent = _store(), _fio_agent()
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store,
                          fio_baseline=_HDD_BASE)
    summ = store.state["et6b1"]["qual"]["steps"]["fio"]["summary"]
    assert summ["flagged"] == ["sdc"]
    assert summ["judge"]["sdc"]["verdict"] == "outside"
    assert summ["judge"]["sda"]["verdict"] == "ok"
    assert summ["judge"]["sdc"]["serial"] == "WX01DSV4"
    assert summ["devices"] == ["sda", "sdb", "sdc"]       # the agent's summary is kept
    # a second poll of the same run keeps the first judgement (an empty baseline
    # would otherwise drop the history range)
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store, fio_baseline={})
    again = store.state["et6b1"]["qual"]["steps"]["fio"]["summary"]
    assert again["flagged"] == ["sdc"] and again["judge"] == summ["judge"]
    # a new run is judged afresh
    agent._run_id = "r2"
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store, fio_baseline={})
    fresh = store.state["et6b1"]["qual"]["steps"]["fio"]["summary"]
    assert summ["judge"]["sdc"]["expected"] == {"lat": "≤ 5100 µs"}
    assert fresh["judge"]["sdc"]["expected"] == {}       # no history group this time


def test_poll_target_does_not_judge_a_running_or_skipped_fio():
    store = _store()
    agent = _fio_agent("running")
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store,
                          fio_baseline=_HDD_BASE)
    assert "summary" not in store.state["et6b1"]["qual"]["steps"]["fio"]
    store = _store()
    agent = FakeAgent(stages=[{"name": "fio", "status": "skip"}],
                      summaries={"fio": {"reason": "no physical storage media"}})
    host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store,
                          fio_baseline=_HDD_BASE)
    assert store.state["et6b1"]["qual"]["steps"]["fio"]["summary"] == {
        "reason": "no physical storage media"}


# --- effective (per-node) order, 2026-11-06 ---------------------------------
def test_build_result_carries_sales_and_customer():
    from flax_post.observe import host_qual
    r = host_qual.build_result({"port": "et7b1", "order_no": "RRORD-1033", "sales": "S-1",
                                "customer": "Acme", "serial": "SN1"},
                               {}, {"run_id": "r1"}, {}, {"verdict": "pass"})
    assert (r["order_no"], r["sales"], r["customer"]) == ("RRORD-1033", "S-1", "Acme")


def test_profile_prefers_override_then_target_population():
    from flax_post.observe import host_qual

    class _S:
        def __init__(self, ov): self.ov = ov
        def read_state(self): return {"et7b1": {"pop_override": self.ov}}
        def read_settings(self): return {"population": "ovh-bare-tp"}
    t = {"port": "et7b1", "population": "acme-leo"}
    assert host_qual._read_profile_for(t, _S("a")) == "a"
    assert host_qual._read_profile_for(t, _S(None)) == "acme-leo"
    assert host_qual._read_profile_for({"port": "et7b1"}, _S(None)) == "ovh-bare-tp"


# ---- site-wide siblings (addendum 2026-11-13): post_fio_drive rows + peers ----
import pathlib as _pl
_FIX = _pl.Path(__file__).resolve().parent / "fixtures" / "fio"
_SMALL = ("SAMSUNG MZVL4256HBJD-00BH1", 256.0)


def _nvme_agent():
    return FakeAgent(status_body={"status": "running", "current": "iperf", "done_n": 6,
                                  "total_n": 12, "verdict": None, "pct": 50},
                     stages=[{"name": "inventory", "status": "pass"}, {"name": "fio", "status": "pass"}],
                     arts={("inventory", "alldisks"): (_FIX / "two-nvme.alldisks.txt").read_text(),
                           ("fio", "fio"): (_FIX / "two-nvme.fio.txt").read_text()},
                     summaries={"fio": {"devices": ["nvme0n1", "nvme1n1"], "smart_excluded": {}}})


def _peer_store(peers=None, boom=False):
    s = _store()
    s.written, s.asked = [], []
    def write_fio_drives(bmc_mac, run_id, rows):
        s.written.append((bmc_mac, run_id, rows)); return len(rows)
    def fio_peers(keys, exclude):
        s.asked.append((sorted(keys, key=str), sorted(exclude)))
        if boom:
            raise RuntimeError("relation post_fio_drive does not exist")
        return peers or {}
    s.write_fio_drives, s.fio_peers = write_fio_drives, fio_peers
    return s


def test_fio_judge_uses_site_peers():
    store = _peer_store({_SMALL: {("bw", "mbps"): [2450.0, 2640.0, 2770.0]}})
    host_qual.poll_target(TARGET, make_client=lambda ip: _nvme_agent(), store=store, fio_baseline={})
    summ = store.state["et6b1"]["qual"]["steps"]["fio"]["summary"]
    assert summ["flagged"] == ["nvme0n1"] and summ["judge"]["nvme0n1"]["verdict"] == "outside"
    assert summ["judge"]["nvme1n1"]["verdict"] == "no baseline"
    (mac, run, rows), = store.written
    assert mac == TARGET["bmc_mac"] and len(rows) == 8
    keys, excl = store.asked[0]
    assert _SMALL in keys and excl == ["TESTSN-FIO-0", "TESTSN-FIO-1"]
    # the same run polled again: no second write, no second query
    host_qual.poll_target(TARGET, make_client=lambda ip: _nvme_agent(), store=store, fio_baseline={})
    assert len(store.written) == 1 and len(store.asked) == 1


def test_fio_judge_without_the_table_still_judges():  # RF4
    store = _peer_store(boom=True)
    host_qual.poll_target(TARGET, make_client=lambda ip: _nvme_agent(), store=store, fio_baseline={})
    summ = store.state["et6b1"]["qual"]["steps"]["fio"]["summary"]
    assert {j["verdict"] for j in summ["judge"].values()} == {"no baseline"}
