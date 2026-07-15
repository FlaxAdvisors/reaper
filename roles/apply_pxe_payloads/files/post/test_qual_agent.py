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
