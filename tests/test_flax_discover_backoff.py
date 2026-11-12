from flax_discover.backoff import MatchBackoff


def test_first_attempt_is_due():
    bo = MatchBackoff(base_secs=30, max_secs=3600)
    assert bo.due("m", now=0.0) is True


def test_backoff_doubles_and_caps():
    bo = MatchBackoff(base_secs=30, max_secs=100)
    bo.record_miss("m", now=0.0)
    assert bo.due("m", now=29.0) is False
    assert bo.due("m", now=30.0) is True       # 30s interval elapsed
    bo.record_miss("m", now=30.0)              # interval -> 60
    assert bo.due("m", now=89.0) is False
    assert bo.due("m", now=90.0) is True
    bo.record_miss("m", now=90.0)              # 120 -> capped at 100
    assert bo.due("m", now=189.0) is False
    assert bo.due("m", now=190.0) is True


def test_reset_makes_due_now():
    bo = MatchBackoff(base_secs=30, max_secs=100)
    bo.record_miss("m", now=0.0)
    assert bo.due("m", now=5.0) is False
    bo.reset("m")
    assert bo.due("m", now=5.0) is True


def test_clear_all_resets_everyone():
    bo = MatchBackoff(base_secs=30, max_secs=100)
    bo.record_miss("a", now=0.0)
    bo.record_miss("b", now=0.0)
    bo.clear_all()
    assert bo.due("a", now=1.0) is True
    assert bo.due("b", now=1.0) is True
