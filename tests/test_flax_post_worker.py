"""observe/worker: one iteration of a slot worker over fake deps."""
import copy

from flax_post.observe import ladder as L, worker

REC = {"port": "et6b1", "bmc_ip": "10.0.0.6", "host_ip": "10.0.0.5", "bmc_mac": "aa:bb",
       "serial": "SN1", "order_no": "RRORD-003", "bmc_pinged": True, "power_on": "off",
       "host_leased": False, "verdict": None, "launch_at": None, "fw_gates": False,
       "fw": {"bmc": {"phase": "up_to_date"}, "bios": {}, "nic": {}}, "ladder": {}}


class Deps:
    def __init__(self, rec=REC, now=100.0):
        self.rec = dict(rec); self.now = now; self.acts = []; self.written = []
        self.ev = {}; self.polls = []; self.holds = False; self.claimed = set()
        self.events = []                      # ("act", name) / ("write", ladder), in order
        self.power_result = {"ok": True, "output": "Chassis Power Control: Up/On"}
    def record(self, port): return self.rec if port == "et6b1" else None
    def hold(self, port): return self.holds
    def holds_claim(self, port): return port in self.claimed
    def evidence(self, kind, rec, lad): return self.ev.get(kind)
    def act(self, action, rec, lad):
        self.acts.append(action); self.events.append(("act", action))
        if action == "claim":
            self.claimed.add(rec["port"]); return True
        if action == "unclaim":
            self.claimed.discard(rec["port"]); return True
        return self.power_result if action in ("power-on", "pxe-cycle") else None
    def poll_agent(self, rec, launch):
        self.polls.append(launch); return {"agent": {"reachable": self.ev.get("agent", False)}}
    def write_ladder(self, port, lad):
        self.written.append((port, copy.deepcopy(lad)))
        self.events.append(("write", copy.deepcopy(lad)))
        self.rec["ladder"] = copy.deepcopy(lad)
    def clock(self): return self.now


def test_snapshot_from_record():
    s = worker.snapshot_from_record(dict(REC, fw={"bmc": {"phase": "flashing"}, "bios": {}, "nic": {}}),
                                    allowed=True, hold=True)
    assert s == {"bmc_pinged": True, "power_on": "off", "host_leased": False, "verdict": None,
                 "hold": True, "fw_flashing": True, "fw_gates": False, "allowed": True,
                 "launch_at": None, "battery_done": False, "fw_steps": None}


def test_snapshot_carries_the_records_firmware_steps():
    fw = {"bmc-checked": "done", "bmc-updated": "done"}
    s = worker.snapshot_from_record(dict(REC, steps={"Firmware": fw, "Discover": {}}), allowed=True, hold=False)
    assert s["fw_steps"] == fw


def test_real_deps_poll_agent_uses_the_ladders_frozen_firmware_steps(monkeypatch):
    """Once fw-gates passed, the verdict snapshot must see the Firmware map the
    gate proved, not a later daemon regression (et28b4 2026-09-12)."""
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    captured = {}
    monkeypatch.setattr(worker.host_qual, "poll_target",
                        lambda target, **kw: captured.update(target=target) or {"agent": {"reachable": True}})
    frozen = {"bmc-checked": "done", "bmc-updated": "done"}
    live = {"bmc-checked": "cur", "bmc-updated": "pending"}
    rec = {"port": "et6b1", "host_ip": "10.0.0.5", "bmc_ip": "10.0.0.6", "bmc_mac": "aa:bb",
           "fw_gates": False, "phase": "Firmware",
           "steps": {"Discover": {"power-on": "done"}, "Firmware": live},
           "ladder": {"rung": "qualify", "fw_steps": frozen}}
    deps.poll_agent(rec, True)
    assert captured["target"]["steps"]["Firmware"] == frozen
    assert captured["target"]["steps"]["Discover"] == {"power-on": "done"}
    assert rec["steps"]["Firmware"] == live                      # the record itself is untouched


def test_real_deps_poll_agent_launch_phase_follows_the_ladder_past_fw_gates(monkeypatch):
    """The launcher's gate is target["phase"] == "Qualify". Once the ladder is
    at/after agent-reachable the gate was proven; a live fw_gates=False (fwd's
    transient `unreachable`) must not stop the launch retry (et27b4 2026-09-12)."""
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    captured = {}
    monkeypatch.setattr(worker.host_qual, "poll_target",
                        lambda target, **kw: captured.update(target=target) or {"agent": {"reachable": False}})
    rec = {"port": "et6b1", "host_ip": "10.0.0.5", "bmc_ip": "10.0.0.6", "bmc_mac": "aa:bb",
           "fw_gates": False, "phase": "Firmware", "ladder": {"rung": "agent-reachable"}}
    deps.poll_agent(rec, True)
    assert captured["target"]["phase"] == "Qualify"
    rec2 = dict(rec, ladder={"rung": "fw-gates"})
    deps.poll_agent(rec2, True)
    assert captured["target"]["phase"] == "Firmware"


def test_empty_slot_writes_nothing():
    d = Deps()
    assert worker.iterate_once("et9b9", d, True, 100.0) == (None, 15) and d.written == []


def test_empty_slot_drops_a_claim_this_worker_took():
    """A blade pulled mid-boot must not leak the claim sentinel: the empty-slot
    early return unclaims when (and only when) this worker holds the claim."""
    d = Deps()
    worker.iterate_once("et6b1", d, True, 100.0)                 # bmc-pinged -> power-on
    worker.iterate_once("et6b1", d, True, 101.0)                 # claim + power-on
    assert d.claimed == {"et6b1"}
    d.record = lambda port: None                                 # blade pulled
    assert worker.iterate_once("et6b1", d, True, 102.0) == (None, 15)
    assert d.acts[-1] == "unclaim" and d.claimed == set()
    # ...and nothing further once it is gone
    n = len(d.acts)
    worker.iterate_once("et6b1", d, True, 103.0)
    assert len(d.acts) == n


def test_first_iterations_probe_fwd_then_power_on_in_order():
    d = Deps()
    lad, _ = worker.iterate_once("et6b1", d, True, 100.0)
    assert lad["rung"] == "power-on" and d.acts == ["probe-fwd"]
    lad, _ = worker.iterate_once("et6b1", d, True, 101.0)
    assert d.acts == ["probe-fwd", "sol-mark:power-on", "claim", "power-on"]
    assert lad["power_on_pending"] is True and d.written[-1][1] == lad


def test_rejected_power_on_faults_and_unclaims():
    d = Deps(); d.power_result = {"ok": False, "output": "Error: Unable to establish IPMI v2 / RMCP+ session"}
    worker.iterate_once("et6b1", d, True, 100.0)
    lad, _ = worker.iterate_once("et6b1", d, True, 101.0)
    assert lad["fault"]["rung"] == "power-on" and "rejected" in lad["fault"]["reason"]
    assert d.acts[-1] == "unclaim" and lad["power_on_pending"] is False


def test_not_allowed_slot_observes_but_never_powers_or_launches():
    d = Deps()
    worker.iterate_once("et6b1", d, False, 100.0)
    lad, _ = worker.iterate_once("et6b1", d, False, 101.0)
    assert "power-on" not in d.acts and lad["rung"] == "power-on"
    # already-on blade walks the ladder; poll-agent runs with launch disabled
    d.rec.update(power_on="on", host_leased=True, fw_gates=True)
    d.ev.update(ping=True, ssh=True, agent=True, bmcdata={"board_mfg": "W", "product": "TP", "serial": "S"},
                **{"ssh-boot": {"ok": True}})                   # already on: host-ssh asks for boot proof
    for t in range(102, 111):
        lad, _ = worker.iterate_once("et6b1", d, False, float(t))
    assert lad["rung"] in ("agent-reachable", "qualify") and d.polls and all(l is False for l in d.polls)


def test_evidence_is_asked_only_for_the_current_rung():
    d = Deps(rec=dict(REC, power_on="on"))
    asked = []
    d.evidence = lambda kind, rec, lad: (asked.append(kind), {"ok": True} if kind == "ssh-boot" else True)[1]
    worker.iterate_once("et6b1", d, True, 100.0)          # bmc-pinged -> power-on
    worker.iterate_once("et6b1", d, True, 101.0)          # already on -> host-pinged
    worker.iterate_once("et6b1", d, True, 102.0)          # ping
    worker.iterate_once("et6b1", d, True, 103.0)          # ssh + boot proof (already on)
    worker.iterate_once("et6b1", d, True, 104.0)          # bmc data read
    assert asked == ["ping", "ssh-boot", "bmcdata"]
    assert d.acts[-3:-1] == ["probe-biosd", "probe-nicd"]


def test_bmc_ready_evidence_uses_ping_then_a_fru0_read(monkeypatch):
    from flax_post.observe import ipmi
    monkeypatch.setattr(ipmi, "_sleep", lambda s: None)
    calls = []
    fru = "Board Mfg             : Wistron\nBoard Product         : Tioga Pass Single Side\nProduct Serial        : WTH1\n"
    run = lambda ip, u, p, args: calls.append((ip, u, args)) or fru
    out = ipmi.bmc_data_check("10.0.0.6", [{"bmcuser": "u", "bmcpass": "p"}], run, ping=lambda ip: True, family_map={})
    assert (out["state"], out["board_mfg"], out["board_product"], out["serial"], out["serial_field"]) == \
        ("ok", "Wistron", "Tioga Pass Single Side", "WTH1", "Product Serial")
    assert calls == [("10.0.0.6", "u", ["fru", "print", "0"])]
    # pings but IPMI not back: no data, no evidence
    assert ipmi.bmc_data_check("10.0.0.6", [{"bmcuser": "u", "bmcpass": "p"}],
                               lambda *a: (_ for _ in ()).throw(RuntimeError("timeout")),
                               ping=lambda ip: True, family_map={}) is None
    # answers with a FRU 0 header and nothing else (blackout tail): absent, re-read, returned
    calls.clear()
    hdr = lambda ip, u, p, args: calls.append(args) or "FRU Device Description : Builtin FRU Device (ID 0)\n"
    out = ipmi.bmc_data_check("10.0.0.6", [{"bmcuser": "u", "bmcpass": "p"}], hdr, ping=lambda ip: True, family_map={})
    assert out["state"] == "absent" and len(calls) == ipmi.FRU0_READS
    # no ping: no read attempted
    calls.clear()
    assert ipmi.bmc_data_check("10.0.0.6", [{"bmcuser": "u", "bmcpass": "p"}], run, ping=lambda ip: False) is None
    assert calls == []


def test_real_deps_evidence_bmcdata_calls_the_bmc_check(monkeypatch):
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    monkeypatch.setattr(worker, "_bmc_data", lambda ip: {"board_mfg": "W", "product": "TP", "serial": "S"} if ip == "10.0.0.6" else None)
    assert deps.evidence("bmcdata", {"bmc_ip": "10.0.0.6"}, {}) == {"board_mfg": "W", "product": "TP", "serial": "S"}
    assert deps.evidence("bmcdata", {"bmc_ip": None}, {}) is None


_SSH_BOOT_DNSMASQ = "Sep 14 08:44:01 dnsmasq-tftp[2771]: sent /srv/tftpboot/efi64/ipxe.efi to 172.25.24.2\n"
_SSH_BOOT_NGINX = (
    '172.25.24.2 - - [14/Sep/2026:08:45:08 +0000] "GET /boot.ipxe HTTP/1.1" 200 47 "-" "iPXE/1.21.1+ (gc1834)"\n'
    '172.25.24.2 - - [14/Sep/2026:08:46:20 +0000] "GET /install//suse/live/test/LiveLeap.x86_64-3.0.0.iso HTTP/1.1"'
    ' 200 2861563904 "-" "curl/8.12.1"\n')


def _ssh_boot_env(monkeypatch, tmp_path, ssh_result):
    from flax_post.biosd import creds, driver
    from flax_post.observe import bootlog
    monkeypatch.setenv("TZ", "UTC"); __import__("time").tzset()
    d = tmp_path / "dnsmasq.log"; d.write_text(_SSH_BOOT_DNSMASQ)
    n = tmp_path / "access.log"; n.write_text(_SSH_BOOT_NGINX)
    monkeypatch.setattr(bootlog, "DNSMASQ_LOG", str(d))
    monkeypatch.setattr(bootlog, "NGINX_ACCESS_LOG", str(n))
    monkeypatch.setattr(creds, "load_host_creds", lambda path=None: ("root", "pw"))
    calls = []
    monkeypatch.setattr(driver, "run_over_ssh",
                        lambda user, pw, ip, script, timeout=300: (calls.append((ip, script)), ssh_result)[1])
    monkeypatch.setattr(worker.time, "time", lambda: 1789379164.0)       # bang clock, 2026-09-14 09:46:04Z
    return calls


def test_real_deps_ssh_boot_evidence_reads_uptime_on_the_bang_clock_and_scans_the_logs(monkeypatch, tmp_path):
    calls = _ssh_boot_env(monkeypatch, tmp_path, (0, "3600.50 7100.25\n"))
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    ev = deps.evidence("ssh-boot", {"host_ip": "172.25.24.2"}, {"born_at": 1789362000.0})   # 05:00:00Z
    assert calls == [("172.25.24.2", "cat /proc/uptime")]
    assert ev == {"ok": True, "kernel_boot": 1789375563.5,                                    # 08:46:03.5Z
                  "tftp": [1789375441.0], "ipxe": [1789375508.0], "iso": [1789375580.0]}


def test_real_deps_ssh_boot_evidence_failed_ssh_is_not_ok(monkeypatch, tmp_path):
    _ssh_boot_env(monkeypatch, tmp_path, (255, "Permission denied"))
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    assert deps.evidence("ssh-boot", {"host_ip": "172.25.24.2"}, {"born_at": 1789362000.0}) == {"ok": False}


def test_real_deps_ssh_boot_evidence_without_host_ip_does_not_ssh(monkeypatch, tmp_path):
    calls = _ssh_boot_env(monkeypatch, tmp_path, (0, "1.0 1.0\n"))
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    assert deps.evidence("ssh-boot", {"host_ip": None}, {"born_at": 1.0}) == {"ok": False} and calls == []


def test_allowlist():
    assert worker.allowed("et6b1", []) is True
    assert worker.allowed("et6b1", ["et6b1", "et7b1"]) is True
    assert worker.allowed("et8b1", ["et6b1"]) is False


def test_feed_serves_latest_records():
    calls = []
    def fetch():
        calls.append(1); return [{"port": "et6b1", "x": len(calls)}, {"port": None, "empty": True}]
    feed = worker.SlotFeed(fetch, interval_s=999)
    feed.refresh()
    assert feed.get("et6b1") == {"port": "et6b1", "x": 1} and feed.get("et9b9") is None


def test_evidence_error_is_swallowed_and_budget_still_expires_into_fault():
    """A raising deps.evidence() (e.g. a missing creds file) must not abort the
    iteration: advance() still runs with no evidence for this step, so the
    ladder is written and, once the rung's budget expires, faults+unclaims
    exactly as a plain timeout would."""
    d = Deps(rec=dict(REC, power_on="on"))
    def raising_evidence(kind, rec, lad):
        if kind == "ping":
            raise RuntimeError("host cred file missing")
        return d.ev.get(kind)
    d.evidence = raising_evidence
    worker.iterate_once("et6b1", d, True, 100.0)             # bmc-pinged -> power-on
    lad, _ = worker.iterate_once("et6b1", d, True, 101.0)    # already on -> host-pinged
    assert lad["rung"] == "host-pinged"
    since = lad["since"]

    lad, _ = worker.iterate_once("et6b1", d, True, 102.0)    # ping raises; must not raise here
    assert lad is not None and lad["rung"] == "host-pinged" and lad.get("fault") is None

    budget = L.budget_s("host-pinged")
    lad, _ = worker.iterate_once("et6b1", d, True, since + budget + 1)
    assert lad["fault"]["rung"] == "host-pinged"
    assert "unclaim" in d.acts


class FakeFeed:
    def __init__(self, initial=None):
        self._recs = dict(initial or {})
    def get(self, port):
        return self._recs.get(port)
    def set(self, port, rec):
        self._recs[port] = rec


class FakeStore:
    def __init__(self):
        self.calls = []
    def set_state(self, port, **kw):
        self.calls.append((port, kw))


def test_real_deps_record_prefers_last_written_ladder_over_stale_feed():
    feed = FakeFeed({"et6b1": {"port": "et6b1", "bmc_ip": "10.0.0.6",
                               "ladder": {"rung": "bmc-pinged"}}})
    deps = worker.RealDeps(feed, store=FakeStore())
    new_lad = {"rung": "power-on", "since": 100.0}
    deps.write_ladder("et6b1", new_lad)

    rec = deps.record("et6b1")
    assert rec["ladder"] == new_lad
    # the feed itself is still serving the old, stale copy
    assert feed.get("et6b1")["ladder"] == {"rung": "bmc-pinged"}


def test_real_deps_record_lets_human_reset_win_and_forgets_cache():
    feed = FakeFeed({"et6b1": {"port": "et6b1", "bmc_ip": "10.0.0.6",
                               "ladder": {"rung": "bmc-pinged"}}})
    deps = worker.RealDeps(feed, store=FakeStore())
    deps.write_ladder("et6b1", {"rung": "power-on", "since": 100.0})

    feed.set("et6b1", {"port": "et6b1", "bmc_ip": "10.0.0.6",
                       "ladder": {"rung": "tftp-seen", "human_power_on": True}})
    rec = deps.record("et6b1")
    assert rec["ladder"] == {"rung": "tftp-seen", "human_power_on": True}

    # cache forgotten: the next feed copy (no human_power_on) is trusted as-is,
    # NOT overridden by the power-on ladder cached before the lane's reset
    feed.set("et6b1", {"port": "et6b1", "bmc_ip": "10.0.0.6", "ladder": {"rung": "tftp-seen"}})
    rec2 = deps.record("et6b1")
    assert rec2["ladder"] == {"rung": "tftp-seen"}


def test_real_deps_record_forgets_cache_when_slot_goes_empty():
    feed = FakeFeed({"et6b1": {"port": "et6b1", "bmc_ip": "10.0.0.6",
                               "ladder": {"rung": "bmc-pinged"}}})
    deps = worker.RealDeps(feed, store=FakeStore())
    deps.write_ladder("et6b1", {"rung": "power-on", "since": 100.0})

    feed.set("et6b1", None)
    assert deps.record("et6b1") is None

    # the slot reappears with the ORIGINAL (pre-write) ladder; the dropped
    # cache must not resurrect the power-on ladder over this fresh copy
    feed.set("et6b1", {"port": "et6b1", "bmc_ip": "10.0.0.6", "ladder": {"rung": "bmc-pinged"}})
    rec = deps.record("et6b1")
    assert rec["ladder"] == {"rung": "bmc-pinged"}


def test_real_deps_poll_agent_qualify_phase_and_launch_gate(monkeypatch):
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    captured = {}
    def fake_poll_target(target, **kw):
        captured["target"] = target; captured["kw"] = kw
        return {"agent": {"reachable": True}}
    monkeypatch.setattr(worker.host_qual, "poll_target", fake_poll_target)
    rec = {"port": "et6b1", "host_ip": "10.0.0.5", "bmc_ip": "10.0.0.6", "bmc_mac": "aa:bb",
          "serial": "SN1", "order_no": "RRORD-003", "fw_gates": True, "phase": "Firmware",
          "steps": {"Discover": {"power-on": "done"}, "Firmware": {"bmc-checked": "done"}}}

    out = deps.poll_agent(rec, True)
    assert out == {"agent": {"reachable": True}}
    assert captured["target"]["phase"] == "Qualify"
    # the record's step maps ride along so the verdict can latch Discover/Firmware
    assert captured["target"]["steps"] == rec["steps"]
    assert captured["kw"]["launch_agent"] is worker.host_qual._default_launch_agent

    deps.poll_agent(rec, False)
    assert captured["kw"]["launch_agent"] is None


def test_real_deps_poll_agent_no_host_ip_short_circuits(monkeypatch):
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    called = []
    monkeypatch.setattr(worker.host_qual, "poll_target", lambda *a, **k: called.append(1))
    rec = {"port": "et6b1", "host_ip": None, "bmc_ip": "10.0.0.6"}
    assert deps.poll_agent(rec, True) == {"agent": {"reachable": False}}
    assert called == []


def test_unchanged_step_writes_nothing_and_sleeps_the_idle_interval():
    """A slot parked at power-on (off, not allowlisted) produces an identical
    ladder every 2s; that must be neither an UPDATE post_state nor a 2s sleep."""
    parked = dict(L.new_ladder(50.0), rung="power-on")
    d = Deps(rec=dict(REC, ladder=parked))
    lad, interval = worker.iterate_once("et6b1", d, False, 100.0)
    assert lad == parked and d.written == [] and interval == 15


def test_changed_step_writes_and_sleeps_the_rung_interval():
    d = Deps(rec=dict(REC, power_on="on"))
    lad, interval = worker.iterate_once("et6b1", d, True, 100.0)   # bmc-pinged -> power-on
    assert d.written and d.written[-1][1] == lad
    assert interval == L.interval_s(lad) == 2


def test_a_step_that_only_acts_still_sleeps_the_rung_interval():
    """No ladder change but an action taken (the agent poll on a qualifying
    slot) is work in flight, not idle."""
    lad0 = dict(L.new_ladder(50.0), rung="qualify")
    d = Deps(rec=dict(REC, power_on="on", host_leased=True, fw_gates=True, ladder=lad0))
    lad, interval = worker.iterate_once("et6b1", d, True, 100.0)
    assert d.polls == [True] and d.written == [] and lad == lad0
    assert interval == L.interval_s(lad0) == 10


def test_power_on_pending_is_persisted_before_the_chassis_is_touched():
    """The IPMI power lane reads `prior` at the start of its pass: if the flag
    lands only after `powertriage` returns (120s), a pass that straddles the
    power-on sees no flag and misreads the engine's own power-on as a human's,
    clobbering done/qual/pop and rotating the SOL capture."""
    d = Deps()
    worker.iterate_once("et6b1", d, True, 100.0)      # bmc-pinged -> power-on
    d.events.clear()
    worker.iterate_once("et6b1", d, True, 101.0)      # issues the power-on
    i_write = next(i for i, e in enumerate(d.events)
                   if e[0] == "write" and e[1].get("power_on_pending") is True)
    i_power = next(i for i, e in enumerate(d.events) if e == ("act", "power-on"))
    i_mark = next(i for i, e in enumerate(d.events) if e == ("act", "sol-mark:power-on"))
    assert i_write < i_mark < i_power


def test_real_deps_record_adopts_a_newer_ladder_reset_at_over_the_cache():
    """set_state merges vars at the top level, so the lane's ladder reset and
    the worker's cached ladder are last-writer-wins on one key. The scalar
    ladder_reset_at is the tiebreak that does not contend."""
    feed = FakeFeed({"et6b1": {"port": "et6b1", "bmc_ip": "10.0.0.6",
                               "ladder": {"rung": "bmc-pinged"}}})
    deps = worker.RealDeps(feed, store=FakeStore())
    deps.write_ladder("et6b1", {"rung": "host-pinged", "since": 100.0})

    # the lane observed a human off->on at 140.0; the feed's `ladder` is still
    # the worker's own copy (the lane's reset was overwritten on that key)
    feed.set("et6b1", {"port": "et6b1", "bmc_ip": "10.0.0.6", "ladder_reset_at": 140.0,
                       "ladder": {"rung": "host-pinged", "since": 100.0}})
    assert deps.record("et6b1")["ladder"] == L.human_reset(140.0)

    # cache dropped: the adopted reset is the new baseline, the feed is trusted
    feed.set("et6b1", {"port": "et6b1", "bmc_ip": "10.0.0.6", "ladder_reset_at": 140.0,
                       "ladder": {"rung": "tftp-seen", "since": 140.0}})
    assert deps.record("et6b1")["ladder"] == {"rung": "tftp-seen", "since": 140.0}


def test_real_deps_record_ignores_a_ladder_reset_at_older_than_the_cache():
    feed = FakeFeed({"et6b1": {"port": "et6b1", "bmc_ip": "10.0.0.6"}})
    deps = worker.RealDeps(feed, store=FakeStore())
    deps.write_ladder("et6b1", {"rung": "host-pinged", "since": 100.0})
    feed.set("et6b1", {"port": "et6b1", "bmc_ip": "10.0.0.6", "ladder_reset_at": 90.0,
                       "ladder": {"rung": "bmc-pinged", "since": 10.0}})
    assert deps.record("et6b1")["ladder"] == {"rung": "host-pinged", "since": 100.0}


def test_real_deps_unclaim_calls_through_even_when_not_in_claimed(monkeypatch):
    """After a flax-post-observe restart, /run/flax/bmc-fw-active/<port> can
    still be ours while the in-process _claimed set is empty (fresh
    process). act('unclaim') must call through to claims.unclaim regardless
    — that function is itself marker-guarded against a foreign file — or the
    port could never be dropped again."""
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    calls = []
    monkeypatch.setattr(worker.claims, "unclaim",
                        lambda port, claim_dir=None: (calls.append(port), True)[1])
    rec = {"port": "et6b1", "bmc_ip": "10.0.0.6"}
    assert "et6b1" not in deps._claimed
    out = deps.act("unclaim", rec, None)
    assert out is True and calls == ["et6b1"]
    assert "et6b1" not in deps._claimed


def test_idle_interval_only_when_no_evidence_is_awaited():
    """A step that neither acts nor changes the ladder idles at 15s only
    when the rung is not waiting on evidence; an evidence rung (tftp-seen)
    with nothing new yet keeps its own tight interval instead of being
    slowed to the idle interval."""
    parked_tftp = dict(L.new_ladder(50.0), rung="tftp-seen", power_on_at=50.0)
    d = Deps(rec=dict(REC, power_on="on", ladder=parked_tftp))
    lad, interval = worker.iterate_once("et6b1", d, True, 100.0)
    assert lad == parked_tftp and d.written == [] and interval == 2

    parked_power = dict(L.new_ladder(50.0), rung="power-on")
    d2 = Deps(rec=dict(REC, power_on="off", ladder=parked_power))
    lad2, interval2 = worker.iterate_once("et6b1", d2, False, 100.0)
    assert lad2 == parked_power and d2.written == [] and interval2 == 15


# ---- per-daemon probe timeouts (0.9.134) ----
def test_probe_daemon_uses_per_daemon_timeout_and_swallows_timeouts(caplog):
    seen = {}

    class _Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout):
        seen[req.full_url] = timeout
        if "8447" in req.full_url:
            raise TimeoutError("timed out")
        return _Resp()

    import logging
    with caplog.at_level(logging.WARNING, logger="flax-post.worker"):
        assert worker._probe_daemon("probe-fwd", "et6b1", opener=opener) is False
        assert worker._probe_daemon("probe-biosd", "et6b1", opener=opener) is True
    assert seen["http://127.0.0.1:8447/probe/et6b1"] == worker.PROBE_TIMEOUT_S["probe-fwd"] == 60
    assert seen["http://127.0.0.1:8449/probe/et6b1"] == worker.PROBE_TIMEOUT_S["probe-biosd"] == 30
    assert "timed out" in caplog.text and "Traceback" not in caplog.text


def test_probe_daemon_refused_connection_is_a_warning_not_an_exception():
    import urllib.error
    def opener(req, timeout):
        raise urllib.error.URLError("connection refused")
    assert worker._probe_daemon("probe-nicd", "et6b1", opener=opener) is False


def test_bmc_ready_evidence_names_the_baseboard_not_the_last_fru_device():
    from flax_post.observe import ipmi
    dump = ("FRU Device Description : Builtin FRU Device (ID 0)\n Board Mfg             : Wistron\n"
            " Board Product         : Tioga Pass Single Side\n Product Serial        : WTH1\n\n"
            "FRU Device Description : NIC (ID 1)\n Board Mfg             : HPE\n Board Product         : 640SFP28\n\n"
            "FRU Device Description : M.2 (ID 2)\n Board Mfg             : Quanta\n Board Product         : Ava-M.2\n")
    out = ipmi.bmc_data_check("10.0.0.6", [{"bmcuser": "u", "bmcpass": "p"}], lambda *a: dump,
                              ping=lambda ip: True, family_map={})
    assert (out["state"], out["board_mfg"], out["board_product"], out["serial"]) == \
        ("ok", "Wistron", "Tioga Pass Single Side", "WTH1")
    assert out["addons"] == ["NIC", "M.2"]


def test_bmc_ready_evidence_never_takes_the_nic_serial():
    from flax_post.observe import ipmi
    dump = ("FRU Device Description : Builtin FRU Device (ID 0)\n Board Mfg             : Wistron\n"
            " Board Product         : Tioga Pass Single Side\n\n"
            "FRU Device Description : HPE Eth 10/25Gb  (ID 86)\n Board Mfg             : HPE\n"
            " Product Serial        : 1I02270LSS\n")
    out = ipmi.bmc_data_check("10.0.0.6", [{"bmcuser": "u", "bmcpass": "p"}], lambda *a: dump,
                              ping=lambda ip: True, family_map={})
    assert out["state"] == "no_serial" and out["serial"] is None


def test_real_deps_record_adopts_an_occupant_reset_as_a_fresh_ladder():
    """spec 2026-09-14-post-occupant-reset §4.1: the heavy pass reset the slot for a new blade."""
    feed = FakeFeed({"et6b1": {"port": "et6b1", "bmc_ip": "10.0.0.6", "ladder": {"rung": "bmc-pinged"}}})
    deps = worker.RealDeps(feed, store=FakeStore())
    deps.write_ladder("et6b1", {"rung": "done", "since": 100.0})
    feed.set("et6b1", {"port": "et6b1", "bmc_ip": "10.0.0.6", "ladder_reset_at": 140.0,
                       "ladder_reset_kind": "occupant", "ladder_reset_born_at": 60.0,
                       "ladder": {"rung": "done", "since": 100.0}})
    assert deps.record("et6b1")["ladder"] == L.occupant_reset(60.0, 140.0)


def test_real_deps_record_human_kind_still_adopts_human_reset():
    feed = FakeFeed({"et6b1": {"port": "et6b1", "bmc_ip": "10.0.0.6"}})
    deps = worker.RealDeps(feed, store=FakeStore())
    deps.write_ladder("et6b1", {"rung": "host-pinged", "since": 100.0})
    feed.set("et6b1", {"port": "et6b1", "bmc_ip": "10.0.0.6", "ladder_reset_at": 140.0,
                       "ladder_reset_kind": "human", "ladder": {"rung": "host-pinged", "since": 100.0}})
    assert deps.record("et6b1")["ladder"] == L.human_reset(140.0)


# ---- spec 2026-11-11 §6: a human power-on re-arms PXE with one engine-owned cycle ----
def _human_reset_deps():
    d = Deps()
    d.rec["ladder"] = worker.ladder.human_reset(100.0)
    d.rec["power_on"] = "on"
    return d


def test_worker_persists_ladder_before_pxe_cycle():
    d = _human_reset_deps()
    worker.iterate_once("et6b1", d, True, 101.0)
    i_write = next(i for i, e in enumerate(d.events)
                   if e[0] == "write" and e[1].get("last_power_attempt") == 101.0)
    i_cycle = d.events.index(("act", "pxe-cycle"))
    assert i_write < i_cycle


def test_worker_failed_rearm_faults_tftp_seen():
    d = _human_reset_deps()
    d.power_result = {"ok": False, "output": "re-arm PXE: http 401"}
    lad, _ = worker.iterate_once("et6b1", d, True, 101.0)
    assert lad["fault"]["rung"] == "tftp-seen" and lad["fault"]["reason"] == "re-arm PXE: http 401"
    assert d.acts[-1] == "unclaim" and "et6b1" not in d.claimed      # review I3


def test_realdeps_pxe_cycle_sets_pxe_then_cycles(monkeypatch):
    from flax_rungs.result import RungResult
    seen = []
    monkeypatch.setattr(worker.deploy, "load_creds",
                        lambda: worker.deploy.Creds([("u", "p")], ("flax", "x"), [("rf", "p")]))
    monkeypatch.setattr(worker.redfish, "set_boot_pxe", lambda ip, cr, **k: (
        seen.append(("pxe", ip, k.get("persistent"))), RungResult("pass", {"method": "redfish"}))[1])
    monkeypatch.setattr(worker.actions, "run_power", lambda ip, a, **k: (
        seen.append(("power", ip, a)), {"ok": True, "output": "ok"})[1])
    deps = worker.RealDeps(FakeFeed(), store=FakeStore())
    res = deps.act("pxe-cycle", {"port": "et6b1", "bmc_ip": "10.0.0.6"}, {})
    assert res["ok"] is True
    assert seen == [("pxe", "10.0.0.6", False), ("power", "10.0.0.6", "cycle")]


def test_realdeps_pxe_cycle_failed_pxe_never_cycles(monkeypatch):
    from flax_rungs.result import RungResult
    seen = []
    monkeypatch.setattr(worker.deploy, "load_creds",
                        lambda: worker.deploy.Creds([("u", "p")], ("flax", "x"), [("rf", "p")]))
    monkeypatch.setattr(worker.redfish, "set_boot_pxe",
                        lambda ip, cr, **k: RungResult("fail", {"error": "http 401"}))
    monkeypatch.setattr(worker.actions, "run_power", lambda ip, a, **k: seen.append(a))
    res = worker.RealDeps(FakeFeed(), store=FakeStore()).act(
        "pxe-cycle", {"port": "et6b1", "bmc_ip": "10.0.0.6"}, {})
    assert res == {"ok": False, "output": "re-arm PXE: http 401"} and seen == []


def test_worker_restamps_engine_window_after_pxe_cycle():
    """review I1: the cycle can take 40 s+ (Redfish tries + powertriage); the engine window
    must count from when the chassis actually moved, not from advance()."""
    class Slow(Deps):
        def act(self, action, rec, lad):
            res = super().act(action, rec, lad)
            if action == "pxe-cycle":
                self.now += 40.0
            return res
    d = Slow(now=101.0)
    d.rec["ladder"] = worker.ladder.human_reset(100.0)
    d.rec["power_on"] = "on"
    lad, _ = worker.iterate_once("et6b1", d, True, 101.0)
    assert lad["last_power_attempt"] == 141.0 and d.rec["ladder"]["last_power_attempt"] == 141.0
