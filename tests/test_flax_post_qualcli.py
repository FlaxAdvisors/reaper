# tests/test_flax_post_qualcli.py
"""flax-post-qual debug CLI: hit a node agent directly, print status/stages."""
from flax_post import qualcli


class Agent:
    def status(self): return {"status": "running", "current": "iperf", "done_n": 6,
                              "total_n": 11, "verdict": None, "pct": 54}
    def stages(self): return [{"name": "sdr-pre", "status": "pass"},
                              {"name": "iperf", "status": "running"}]


def test_render_lists_steps_and_progress():
    out = qualcli.render(Agent().status(), Agent().stages())
    assert "iperf" in out and "6/11" in out and "sdr-pre" in out and "pass" in out


def test_main_ok_returns_zero(capsys):
    rc = qualcli.main(["10.0.0.5"], make_client=lambda ip: Agent())
    assert rc == 0
    assert "iperf" in capsys.readouterr().out


def test_main_unreachable_returns_two():
    from flax_post.qualclient import QualUnreachable
    class Dead:
        def status(self): raise QualUnreachable("down")
    rc = qualcli.main(["10.0.0.5"], make_client=lambda ip: Dead())
    assert rc == 2
