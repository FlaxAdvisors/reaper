# tests/test_flax_post_fwd_enforce_run_node.py
from flax_post.fwd import enforce


class _Client:
    def __init__(self, power_seq):
        # power_seq: successive get_power_state() returns, e.g. ["Off","On"]
        self._seq = list(power_seq)
        self.pxe_set = False
        self.powered_on = False
    def get_power_state(self):
        v = self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]
        if v is None:
            return None, "BMC unreachable"
        return v, "ok"
    def set_boot_pxe(self):
        self.pxe_set = True
        return True, "ok"
    def power_on(self):
        self.powered_on = True
        return True, "ok"


def _flash_spy(calls):
    def flash_one(port, client, matcher, fetch, set_row, share_base, **kw):
        calls.append(port)
        return "done"
    return flash_one


def test_powered_off_node_gets_pxe_and_power_then_flashes():
    calls = []
    c = _Client(["Off", "On"])
    phase = enforce.run_node("et6b1", c, matcher=None, fetch=None, set_row=lambda *a, **k: None,
                             share_base="s", flash_one=_flash_spy(calls),
                             sleep=lambda s: None, power_wait_s=60, poll_s=10)
    assert c.pxe_set and c.powered_on          # Discover action fired
    assert calls == ["et6b1"]                  # then flashed
    assert phase == "done"


def test_already_on_node_skips_power_and_flashes():
    calls = []
    c = _Client(["On"])
    enforce.run_node("et6b1", c, matcher=None, fetch=None, set_row=lambda *a, **k: None,
                     share_base="s", flash_one=_flash_spy(calls), sleep=lambda s: None)
    assert not c.powered_on                     # no power action needed
    assert calls == ["et6b1"]


def test_power_never_comes_up_records_and_skips_flash():
    calls = []
    c = _Client(["Off"])                        # stays Off forever
    rows = []
    phase = enforce.run_node("et6b1", c, matcher=None, fetch=None,
                             set_row=lambda port, **k: rows.append(k), share_base="s",
                             flash_one=_flash_spy(calls), sleep=lambda s: None,
                             power_wait_s=30, poll_s=10)
    assert calls == []                          # never flashed
    assert phase == "unreachable"
    assert rows and rows[-1].get("phase") == "unreachable"


def test_bmc_dark_on_first_read_is_unreachable():
    calls = []
    rows = []
    c = _Client([None])
    phase = enforce.run_node("et6b1", c, matcher=None, fetch=None,
                             set_row=lambda port, **k: rows.append(k),
                             share_base="s", flash_one=_flash_spy(calls),
                             sleep=lambda s: None)
    assert calls == []                      # never flashed
    assert phase == "unreachable"
    assert not c.pxe_set and not c.powered_on   # no Discover action on a dark BMC
    assert rows and rows[-1].get("phase") == "unreachable"


def test_on_action_records_pxe_and_power_for_off_node():
    actions = []
    c = _Client(["Off", "On"])
    enforce.run_node("et6b1", c, matcher=None, fetch=None,
                     set_row=lambda *a, **k: None, share_base="s",
                     flash_one=_flash_spy([]), sleep=lambda s: None,
                     power_wait_s=60, poll_s=10,
                     on_action=lambda a, ok, det: actions.append((a, ok)))
    assert ("set-pxe", True) in actions
    assert ("power-on", True) in actions


def test_on_action_not_called_for_already_on_node():
    actions = []
    c = _Client(["On"])
    enforce.run_node("et6b1", c, matcher=None, fetch=None,
                     set_row=lambda *a, **k: None, share_base="s",
                     flash_one=_flash_spy([]), sleep=lambda s: None,
                     on_action=lambda a, ok, det: actions.append((a, ok)))
    assert actions == []                    # no Discover action -> no records
