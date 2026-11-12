import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
import qual_agent
from qual_battery import Battery


def test_node_identity_parses_bootif_and_serial():
    def runner(argv, timeout):
        if argv[:2] == ["dmidecode", "-s"]:
            return 0, "SN12345\n"
        return 0, ""
    mac, serial = qual_agent.node_identity(runner, cmdline="BOOTIF=01-aa-bb-cc-dd-ee-ff foo")
    assert mac == "aabbccddeeff" and serial == "SN12345"


def test_supervisor_reruns_after_ack():
    # a battery whose run() just flips to done; after ack it should run again (new run_id)
    b = Battery(runner=lambda a, t: (0, ""),
                stages=[{"name": "x", "fn": lambda r: {"verdict": "pass", "summary": {}, "artifacts": {}}}])
    runs = qual_agent._supervise_once(b)         # runs the battery once
    assert b.status()["status"] == "done"
    first = b.run_id
    b.request_restart(); b.ack_restart()
    qual_agent._supervise_once(b)                # re-run after ack
    assert b.status()["status"] == "done" and b.run_id != first


def test_main_bad_flax_qual_stages_reports_on_health(monkeypatch):
    monkeypatch.setenv("FLAX_QUAL_STAGES", "fio,bogus")
    monkeypatch.setattr(qual_agent, "node_identity", lambda r: ("aa", "SN"))
    seen = {}

    class _H(object):
        def serve_forever(self):
            pass

    def serve(b):
        seen["b"] = b
        return _H()
    qual_agent.main(serve_fn=serve)
    h = seen["b"].health()
    assert h["ok"] is False and h["error"] == "unknown stage bogus"
    assert seen["b"].stages_list() == []


def test_main_env_selects_stages(monkeypatch):
    monkeypatch.setenv("FLAX_QUAL_STAGES", "fio,sdr-pre")
    monkeypatch.setattr(qual_agent, "node_identity", lambda r: ("aa", "SN"))
    seen = {}

    class _H(object):
        def serve_forever(self):
            pass
    monkeypatch.setattr(qual_agent.threading, "Thread",
                        lambda **k: type("T", (), {"start": lambda s: None})())
    qual_agent.main(serve_fn=lambda b: seen.setdefault("b", b) and _H())
    assert [s["name"] for s in seen["b"].stages_list()] == ["sdr-pre", "fio"]
